"""Stage C: guard events are fail-closed.

Coverage:
- Guard *execution* failure (error/timeout/invalid_output) blocks by default,
  and an explicit ``onFailure: continue`` opts out.
- Guard *config-load* failure blocks unconditionally (PreToolUse / PermissionRequest).
- Non-guard events stay non-blocking unless ``onFailure: block`` is set.
- Stop-like events never promote.
"""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from genericagent.hooks.audit import HookAudit
from genericagent.hooks.config import HookRegistry, load_hook_registry
from genericagent.hooks.models import HookEntry, HookHandler, HookResult
from genericagent.hooks.runner import execute_handler
from genericagent.hooks.runtime import HookRuntime


def _agent(runtime):
    return SimpleNamespace(session_id="s", session_path=None, cwd=".", permission_mode="full_access",
                           session_turn_id=0, agent_id=None, agent_type=None, hook_runtime=runtime)


def _rt(registry=None):
    return HookRuntime(registry=registry or HookRegistry(),
                       audit=HookAudit(Path(tempfile.mkdtemp()) / "audit"))


class GuardExecutionContextTests(unittest.TestCase):
    def test_guard_command_error_blocks_by_default(self):
        handler = HookHandler("command", {"command": "definitely-not-a-real-binary-xyz"})
        result = HookResult("PreToolUse", handler, "test", "", "h", "running")
        execute_handler(handler, {"hook_event_name": "PreToolUse"}, result)
        self.assertEqual(result.status, "blocking")
        self.assertTrue(result.blocking)

    def test_guard_command_error_opt_out_with_explicit_continue(self):
        handler = HookHandler("command", {"command": "definitely-not-a-real-binary-xyz", "onFailure": "continue"})
        result = HookResult("PreToolUse", handler, "test", "", "h", "running")
        execute_handler(handler, {"hook_event_name": "PreToolUse"}, result)
        self.assertEqual(result.status, "error")
        self.assertFalse(result.blocking)

    def test_guard_invalid_output_blocks_by_default(self):
        handler = HookHandler("http", {"url": "http://hook.invalid/test"})
        response = SimpleNamespace(status_code=200, text="not-json")
        result = HookResult("PreToolUse", handler, "test", "", "h", "running")
        with mock.patch("requests.post", return_value=response):
            execute_handler(handler, {"hook_event_name": "PreToolUse"}, result)
        self.assertEqual(result.status, "blocking")

    def test_guard_timeout_blocks_by_default(self):
        import time
        def slow_runner(kind, prompt, payload):
            time.sleep(0.08)
            return "late"
        handler = HookHandler("prompt", {"prompt": "x", "timeout": 1})
        handler.options["timeout"] = 0.01
        result = HookResult("PreToolUse", handler, "test", "", "h", "running")
        execute_handler(handler, {"hook_event_name": "PreToolUse"}, result, prompt_runner=slow_runner)
        self.assertEqual(result.status, "blocking")

    def test_non_guard_error_is_non_blocking_by_default(self):
        handler = HookHandler("command", {"command": "definitely-not-a-real-binary-xyz"})
        result = HookResult("PostToolUse", handler, "test", "", "h", "running")
        execute_handler(handler, {"hook_event_name": "PostToolUse"}, result)
        self.assertEqual(result.status, "error")
        self.assertFalse(result.blocking)

    def test_non_guard_error_opt_in_block(self):
        handler = HookHandler("command", {"command": "definitely-not-a-real-binary-xyz", "onFailure": "block"})
        result = HookResult("PostToolUse", handler, "test", "", "h", "running")
        execute_handler(handler, {"hook_event_name": "PostToolUse"}, result)
        self.assertEqual(result.status, "blocking")

    def test_stop_like_never_promotes_even_with_block(self):
        handler = HookHandler("http", {"url": "http://hook.invalid/test", "onFailure": "block"})
        response = SimpleNamespace(status_code=200, text="not-json")
        for event in ("Stop", "SubagentStop", "TaskCompleted", "TeammateIdle"):
            with self.subTest(event=event):
                result = HookResult(event, handler, "test", "", "h", "running")
                with mock.patch("requests.post", return_value=response):
                    execute_handler(handler, {"hook_event_name": event}, result)
                self.assertFalse(result.blocking)


class GuardConfigFailClosedTests(unittest.TestCase):
    def _runtime_with_bad_guard(self, event, bad_entry):
        with tempfile.TemporaryDirectory() as td:
            cfgdir = Path(td) / ".genericagent"
            cfgdir.mkdir()
            (cfgdir / "hooks.json").write_text(json.dumps({"hooks": {event: [bad_entry]}}), encoding="utf-8")
            reg = load_hook_registry(config_dir=cfgdir, home=Path(td) / "home")
            return _rt(reg), reg

    def test_bad_pretooluse_config_blocks(self):
        rt, reg = self._runtime_with_bad_guard("PreToolUse", {"hooks": [{"type": "script", "command": "x"}]})
        self.assertTrue(reg.guard_config_errors.get("PreToolUse"))
        agg = rt.run_event(_agent(rt), "PreToolUse", {"tool_name": "code_run"})
        self.assertTrue(agg.blocked)

    def test_bad_permission_request_config_denies(self):
        rt, reg = self._runtime_with_bad_guard(
            "PermissionRequest", {"hooks": [{"type": "command"}]})  # command missing 'command'
        self.assertTrue(reg.guard_config_errors.get("PermissionRequest"))
        agg = rt.run_event(_agent(rt), "PermissionRequest", {"tool_name": "code_run"})
        self.assertTrue(agg.blocked)
        self.assertEqual(agg.permission_behavior, "deny")

    def test_bad_user_prompt_submit_config_does_not_fail_closed(self):
        # UserPromptSubmit execution failure is fail-closed, but a *config* load
        # error is not in the T4 set, so it must NOT blanket-block.
        rt, reg = self._runtime_with_bad_guard("UserPromptSubmit", {"hooks": [{"type": "script"}]})
        self.assertFalse(reg.guard_config_errors.get("UserPromptSubmit"))
        agg = rt.run_event(_agent(rt), "UserPromptSubmit", {"prompt": "hi"})
        self.assertFalse(agg.blocked)

    def test_agent_handler_on_permission_request_is_not_fail_closed(self):
        rt, reg = self._runtime_with_bad_guard("PermissionRequest", {"hooks": [{"type": "agent", "prompt": "ok?"}]})
        self.assertFalse(reg.guard_config_errors.get("PermissionRequest"))
        agg = rt.run_event(_agent(rt), "PermissionRequest", {"tool_name": "code_run"})
        self.assertFalse(agg.blocked)

    def test_clean_config_has_no_guard_errors(self):
        with tempfile.TemporaryDirectory() as td:
            cfgdir = Path(td) / ".genericagent"
            cfgdir.mkdir()
            (cfgdir / "hooks.json").write_text(json.dumps({"hooks": {"PreToolUse": [
                {"matcher": "code_run", "hooks": [{"type": "command", "command": "python", "args": ["x.py"]}]}
            ]}}), encoding="utf-8")
            reg = load_hook_registry(config_dir=cfgdir, home=Path(td) / "home")
            self.assertEqual(reg.guard_config_errors, {})


if __name__ == "__main__":
    unittest.main()
