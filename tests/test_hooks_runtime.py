import json
import os
import tempfile
import time
import unittest
from unittest import mock
from pathlib import Path
from types import SimpleNamespace

from genericagent.hooks.audit import HookAudit
from genericagent.hooks.config import load_hook_registry
from genericagent.hooks.models import EVENT_NAMES, HookAggregate, HookHandler, HookResult, HookWakeRequest
from genericagent.hooks.runner import execute_handler
from genericagent.hooks.runtime import HookRuntime
from genericagent.hooks.matcher import entry_matches, handler_matches
from genericagent.hooks.models import HookEntry, HookPayload


CC_303_EVENTS = {
    "PreToolUse", "PostToolUse", "PostToolUseFailure", "PostToolBatch",
    "Notification", "UserPromptSubmit", "UserPromptExpansion",
    "SessionStart", "SessionEnd", "Stop", "StopFailure",
    "SubagentStart", "SubagentStop", "PreCompact", "PostCompact",
    "PreModelSwitch", "PostModelSwitch", "PermissionRequest",
    "PermissionDenied", "Setup", "TeammateIdle", "TaskCreated",
    "TaskCompleted", "Elicitation", "ElicitationResult", "ConfigChange",
    "WorktreeCreate", "WorktreeRemove", "InstructionsLoaded",
    "CwdChanged", "FileChanged", "DirectoryAdded", "MessageDisplay",
}


class HookRuntimeTests(unittest.TestCase):
    def test_event_vocabulary_matches_cc_303_exactly(self):
        self.assertEqual(EVENT_NAMES, CC_303_EVENTS)
        self.assertEqual(len(EVENT_NAMES), 33)

    def test_config_loads_all_five_handler_types(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / ".genericagent").mkdir()
            path = Path(td) / ".genericagent" / "hooks.json"
            path.write_text(json.dumps({"hooks": {
                "PreToolUse": [{"matcher": "code_run", "hooks": [{"type": "command", "command": "python", "args": ["x.py"]}]}],
                "PostToolUse": [{"hooks": [{"type": "prompt", "prompt": "review $ARGUMENTS"}]}],
                "SubagentStart": [{"hooks": [{"type": "agent", "prompt": "verify"}]}],
                "SessionEnd": [{"hooks": [{"type": "http", "url": "http://127.0.0.1:1/hook"}]}],
                "TeammateIdle": [{"hooks": [{"type": "mcp_tool", "server": "demo", "tool": "notify", "input": {"x": "${tool_input.path}"}}]}],
            }}), encoding="utf-8")
            registry = load_hook_registry(config_dir=Path(td) / ".genericagent", home=Path(td) / "home")
            self.assertEqual(len(registry.entries), 5)
            self.assertEqual({h.kind for e in registry.entries for h in e.handlers}, {"command", "prompt", "agent", "http", "mcp_tool"})
            self.assertEqual(registry.issues, [])

    def test_unknown_config_event_and_handler_are_reported_not_crashing(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / ".genericagent").mkdir()
            path = Path(td) / ".genericagent" / "hooks.json"
            path.write_text(json.dumps({"hooks": {
                "PreAgentStart": [{"hooks": [{"type": "command", "command": "x"}]}],
                "Stop": [{"hooks": [{"type": "script", "command": "x"}]}],
            }}), encoding="utf-8")
            registry = load_hook_registry(config_dir=Path(td) / ".genericagent", home=Path(td) / "home")
            self.assertEqual(len(registry.entries), 0)
            self.assertEqual(len(registry.issues), 2)

    def test_command_exec_form_gets_json_stdin_and_exact_argv(self):
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / "hook.py"
            out = Path(td) / "out.json"
            script.write_text(
                "import json,sys,pathlib\n"
                "p=json.load(sys.stdin)\n"
                "pathlib.Path(sys.argv[1]).write_text(json.dumps({'event':p['hook_event_name'],'n':len(sys.argv)}),encoding='utf-8')\n",
                encoding="utf-8", newline="\n")
            handler = HookHandler("command", {"command": "python", "args": [str(script), str(out)]})
            result = HookResult("Stop", handler, "test", str(td), "hook_test", "running")
            execute_handler(handler, {"hook_event_name": "Stop", "cwd": td}, result)
            self.assertEqual(result.status, "success")
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(json.loads(out.read_text(encoding="utf-8")), {"event": "Stop", "n": 2})

    def test_parent_paths_and_handler_if_are_two_stage_filters(self):
        payload = HookPayload("PreToolUse", {"tool_name": "Bash", "tool_input": {"command": "git status"}})
        entry = HookEntry("PreToolUse", "Bash", (), "test")
        self.assertTrue(entry_matches(entry, payload))
        self.assertFalse(entry_matches(HookEntry("PreToolUse", "Read", (), "test"), payload))
        self.assertTrue(handler_matches(HookHandler("command", {"command": "x", "if": "Bash(git *)"}), payload))
        self.assertFalse(handler_matches(HookHandler("command", {"command": "x", "if": "Bash(rm *)"}), payload))

    def test_hook_specific_output_must_name_the_same_event(self):
        from genericagent.hooks.runner import _validate_output
        handler = HookHandler("command", {"command": "x"})
        result = HookResult("Stop", handler, "test", "", "hook_test", "success", output={
            "hookSpecificOutput": {"hookEventName": "PreToolUse", "additionalContext": "x"}
        })
        _validate_output(handler, result)
        self.assertEqual(result.status, "invalid_output")

    def test_permission_request_nested_decision_is_aggregated(self):
        aggregate = HookAggregate("PermissionRequest")
        aggregate.add(HookResult("PermissionRequest", HookHandler("command", {"command": "x"}), "test", "", "h", "success", output={
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": {
                    "behavior": "allow",
                    "updatedInput": {"path": "inside.txt"},
                },
            }
        }))
        self.assertEqual(aggregate.permission_behavior, "allow")
        self.assertEqual(aggregate.updated_input, {"path": "inside.txt"})
        self.assertEqual(aggregate.permission_decision, "allow")

    def test_permission_request_invalid_behavior_is_rejected(self):
        from genericagent.hooks.runner import _validate_output
        handler = HookHandler("command", {"command": "x"})
        result = HookResult("PermissionRequest", handler, "test", "", "h", "success", output={
            "hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": {"behavior": "ask"},
            }
        })
        _validate_output(handler, result)
        self.assertEqual(result.status, "invalid_output")
        self.assertIn("decision.behavior", result.error)

    def test_permission_request_agent_handler_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / ".genericagent" / "hooks.json"
            cfg.parent.mkdir()
            cfg.write_text(json.dumps({"hooks": {"PermissionRequest": [{"hooks": [{
                "type": "agent", "prompt": "approve?",
            }]}]}}), encoding="utf-8")
            registry = load_hook_registry(config_dir=cfg.parent, home=Path(td) / "home")
            self.assertEqual(registry.entries, [])
            self.assertTrue(any("agent handler is not supported" in issue.message for issue in registry.issues))

    def test_updated_tool_output_prefers_cc_field_and_allows_null(self):
        aggregate = HookAggregate("PostToolUse")
        aggregate.add(HookResult("PostToolUse", HookHandler("command", {"command": "x"}), "test", "", "h", "success", output={
            "hookSpecificOutput": {"hookEventName": "PostToolUse", "updatedToolOutput": None}
        }))
        self.assertTrue(aggregate.updated_output_set)
        self.assertIsNone(aggregate.updated_output)

    def test_mcp_template_expansion_is_recursive(self):
        captured = {}

        def mcp_call(tool, args):
            captured.update(tool=tool, args=args)
            return "ok"

        handler = HookHandler("mcp_tool", {"server": "s", "tool": "t", "input": {
            "nested": {"path": "${tool_input.path}"},
            "literal": "prefix-${session_id}",
        }})
        result = HookResult("PreToolUse", handler, "test", "", "h", "running")
        execute_handler(handler, {"hook_event_name": "PreToolUse", "session_id": "s1", "tool_input": {"path": "a/b"}}, result, mcp_call=mcp_call)
        self.assertEqual(result.status, "success")
        self.assertEqual(captured["tool"], "mcp__s__t")
        self.assertEqual(captured["args"], {"nested": {"path": "a/b"}, "literal": "prefix-s1"})

    def test_stop_like_events_ignore_on_failure_block(self):
        for event in ("Stop", "SubagentStop", "TaskCompleted", "TeammateIdle"):
            with self.subTest(event=event):
                handler = HookHandler("http", {
                    "url": "http://hook.invalid/test",
                    "onFailure": "block",
                })
                response = SimpleNamespace(status_code=200, text="not-json")
                result = HookResult(event, handler, "test", "", "h", "running")
                with mock.patch("requests.post", return_value=response):
                    execute_handler(handler, {"hook_event_name": event}, result)
                self.assertEqual(result.status, "invalid_output")
                self.assertFalse(result.blocking)

    def test_prompt_runner_can_return_plain_text_context(self):
        handler = HookHandler("prompt", {"prompt": "review"})
        result = HookResult("PostToolUse", handler, "test", "", "h", "running")
        execute_handler(
            handler,
            {"hook_event_name": "PostToolUse"},
            result,
            prompt_runner=lambda _kind, _prompt, _payload: "plain context",
        )
        self.assertEqual(result.status, "success")
        self.assertEqual(result.additional_context, "plain context")

    def test_guard_event_http_still_blocks_on_invalid_json(self):
        handler = HookHandler("http", {
            "url": "http://hook.invalid/test",
            "onFailure": "block",
        })
        response = SimpleNamespace(status_code=200, text="not-json")
        result = HookResult("PreToolUse", handler, "test", "", "h", "running")
        with mock.patch("requests.post", return_value=response):
            execute_handler(handler, {"hook_event_name": "PreToolUse"}, result)
        self.assertEqual(result.status, "blocking")
        self.assertTrue(result.blocking)

    def test_prompt_and_agent_handlers_use_isolated_prompt_runner(self):
        captured = []

        def prompt_runner(kind, prompt, payload):
            captured.append((kind, prompt, payload))
            return {"hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": f"{kind}-ok",
            }}

        for kind in ("prompt", "agent"):
            handler = HookHandler(kind, {"prompt": "check $ARGUMENTS"})
            result = HookResult("PostToolUse", handler, "test", "", "h", "running")
            execute_handler(
                handler,
                {"hook_event_name": "PostToolUse", "tool_name": "file_read"},
                result,
                prompt_runner=prompt_runner,
            )
            self.assertEqual(result.status, "success")
            self.assertEqual(result.additional_context, f"{kind}-ok")
        self.assertEqual([item[0] for item in captured], ["prompt", "agent"])
        self.assertIn("isolated GenericAgent hook verifier", captured[1][1])

    def test_prompt_and_agent_timeout_is_reported(self):
        import time

        def slow_runner(kind, prompt, payload):
            time.sleep(0.08)
            return "late"

        handler = HookHandler("prompt", {"prompt": "x", "timeout": 1})
        # Force a fractional timeout while keeping the config clamp out of the path.
        handler.options["timeout"] = 0.01
        result = HookResult("PostToolUse", handler, "test", "", "h", "running")
        execute_handler(handler, {"hook_event_name": "PostToolUse"}, result, prompt_runner=slow_runner)
        self.assertEqual(result.status, "timeout")

    def test_http_handler_posts_json_and_resolves_allowed_env_headers(self):
        handler = HookHandler("http", {
            "url": "https://hook.invalid/test",
            "headers": {"Authorization": "Bearer $GA_HOOK_TEST_TOKEN"},
            "allowedEnvVars": ["GA_HOOK_TEST_TOKEN"],
        })
        result = HookResult("SessionEnd", handler, "test", "", "h", "running")
        response = SimpleNamespace(status_code=200, text='{"continue": true}')
        with mock.patch.dict(os.environ, {"GA_HOOK_TEST_TOKEN": "secret"}, clear=False), mock.patch(
            "requests.post", return_value=response
        ) as post:
            execute_handler(handler, {"hook_event_name": "SessionEnd", "reason": "exit"}, result)
        self.assertEqual(result.status, "success")
        post.assert_called_once()
        self.assertEqual(post.call_args.kwargs["headers"]["Authorization"], "Bearer secret")
        self.assertEqual(post.call_args.kwargs["json"]["reason"], "exit")

    def test_http_handler_non_2xx_is_non_blocking_by_default(self):
        handler = HookHandler("http", {"url": "https://hook.invalid/test"})
        result = HookResult("SessionEnd", handler, "test", "", "h", "running")
        response = SimpleNamespace(status_code=503, text="unavailable")
        with mock.patch("requests.post", return_value=response):
            execute_handler(handler, {"hook_event_name": "SessionEnd"}, result)
        self.assertEqual(result.status, "error")
        self.assertFalse(result.blocking)

    def test_runtime_records_audit_and_recent_events(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / ".genericagent").mkdir()
            cfg = Path(td) / ".genericagent" / "hooks.json"
            script = Path(td) / "hook.py"
            script.write_text("import sys; sys.stdin.read()\n", encoding="utf-8", newline="\n")
            cfg.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{"type": "command", "command": "python", "args": [str(script)]}]}]}}), encoding="utf-8")
            registry = load_hook_registry(config_dir=Path(td) / ".genericagent", home=Path(td) / "home")
            from genericagent.hooks.audit import HookAudit
            runtime = HookRuntime(registry, audit=HookAudit(Path(td) / "audit"))
            agent = SimpleNamespace(session_id="s", session_path=None, cwd=td, permission_mode="full_access", session_turn_id=1, agent_id=None, agent_type=None)
            aggregate = runtime.run_event(agent, "Stop", {"exit_reason": "completed"})
            self.assertEqual(aggregate.results[0].status, "success")
            self.assertEqual(runtime.recent_events(1)[0]["event"], "Stop")
            snapshot = runtime.status_snapshot()
            self.assertEqual(snapshot["handlers"], 1)
            self.assertEqual(snapshot["events"]["Stop"], "integrated")


    def test_async_command_is_backgrounded_and_audited(self):
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / "slow_hook.py"
            marker = Path(td) / "done.txt"
            nl = chr(10)
            script.write_text(
                nl.join([
                    "import sys,time,pathlib",
                    "sys.stdin.read()",
                    "time.sleep(0.15)",
                    "pathlib.Path(sys.argv[1]).write_text('ok',encoding='utf-8')",
                    "",
                ]),
                encoding="utf-8", newline=nl)
            cfg = Path(td) / ".genericagent" / "hooks.json"
            cfg.parent.mkdir()
            cfg.write_text(json.dumps({"hooks": {"SessionEnd": [{"hooks": [{
                "type": "command", "command": "python", "args": [str(script), str(marker)],
                "async": True,
            }]}]}}), encoding="utf-8")
            registry = load_hook_registry(config_dir=cfg.parent, home=Path(td) / "home")
            runtime = HookRuntime(registry, audit=HookAudit(Path(td) / "audit"))
            agent = SimpleNamespace(session_id="s", session_path=None, cwd=td, permission_mode="full_access", session_turn_id=1, agent_id=None, agent_type=None)
            started = time.perf_counter()
            aggregate = runtime.run_event(agent, "SessionEnd", {"reason": "exit"})
            elapsed = time.perf_counter() - started
            self.assertLess(elapsed, 0.1)
            self.assertTrue(aggregate.results[0].backgrounded)
            deadline = time.time() + 2
            while time.time() < deadline and not marker.exists():
                time.sleep(0.02)
            self.assertTrue(marker.exists())
            deadline = time.time() + 2
            while time.time() < deadline and not runtime.recent_events(5):
                time.sleep(0.02)
            self.assertTrue(runtime.recent_events(5))

    def test_async_rewake_only_on_exit_two_and_queues_wake(self):
        with tempfile.TemporaryDirectory() as td:
            script = Path(td) / "rewake_hook.py"
            nl = chr(10)
            script.write_text(
                nl.join([
                    "import sys",
                    "sys.stdin.read()",
                    "sys.stderr.write('needs another check')",
                    "raise SystemExit(2)",
                    "",
                ]),
                encoding="utf-8", newline=nl)
            cfg = Path(td) / ".genericagent" / "hooks.json"
            cfg.parent.mkdir()
            cfg.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{
                "type": "command", "command": "python", "args": [str(script)],
                "asyncRewake": True, "rewakeMessage": "Retry verification.",
                "rewakeSummary": "verification failed",
            }]}]}}), encoding="utf-8")
            registry = load_hook_registry(config_dir=cfg.parent, home=Path(td) / "home")
            runtime = HookRuntime(registry, audit=HookAudit(Path(td) / "audit"))
            seen = []
            runtime.set_wake_handler(lambda request: seen.append(request) or True)
            agent = SimpleNamespace(session_id="s", session_path=None, cwd=td, permission_mode="full_access", session_turn_id=1, agent_id=None, agent_type=None)
            runtime.run_event(agent, "Stop", {"exit_reason": "completed"})
            deadline = time.time() + 2
            while time.time() < deadline and not seen:
                time.sleep(0.02)
            self.assertEqual(len(seen), 1)
            self.assertIsInstance(seen[0], HookWakeRequest)
            self.assertIn("Retry verification.", seen[0].message)
            self.assertIn("needs another check", seen[0].message)
            self.assertEqual("verification failed", seen[0].summary)
            runtime.close()

    def test_http_url_policy_deny_all_blocks_before_request(self):
        handler = HookHandler("http", {"url": "https://hook.invalid/test"})
        result = HookResult("SessionEnd", handler, "test", "", "h", "running")
        with mock.patch("requests.post") as post:
            execute_handler(handler, {"hook_event_name": "SessionEnd"}, result, allowed_http_urls=[])
        self.assertEqual(result.status, "error")
        post.assert_not_called()

    def test_http_url_policy_wildcard_and_redirect_disabled(self):
        handler = HookHandler("http", {"url": "https://hooks.example.com/a"})
        result = HookResult("SessionEnd", handler, "test", "", "h", "running")
        response = SimpleNamespace(status_code=200, text="{}")
        with mock.patch("requests.post", return_value=response) as post:
            execute_handler(handler, {"hook_event_name": "SessionEnd"}, result, allowed_http_urls=["https://hooks.example.com/*"])
        self.assertEqual(result.status, "success")
        self.assertFalse(post.call_args.kwargs["allow_redirects"])

    def test_http_env_allowlist_intersects_global_policy(self):
        handler = HookHandler("http", {
            "url": "https://hook.invalid/test",
            "headers": {"Authorization": "Bearer $TOKEN", "X-Other": "$OTHER"},
            "allowedEnvVars": ["TOKEN", "OTHER"],
        })
        result = HookResult("SessionEnd", handler, "test", "", "h", "running")
        response = SimpleNamespace(status_code=200, text="{}")
        with mock.patch.dict(os.environ, {"TOKEN": "yes", "OTHER": "no"}, clear=False), mock.patch(
            "requests.post", return_value=response
        ) as post:
            execute_handler(
                handler,
                {"hook_event_name": "SessionEnd"},
                result,
                global_allowed_env_vars=["TOKEN"],
            )
        self.assertEqual("Bearer yes", post.call_args.kwargs["headers"]["Authorization"])
        self.assertEqual("$OTHER", post.call_args.kwargs["headers"]["X-Other"])

    def test_async_fields_are_rejected_on_non_command_handlers(self):
        with tempfile.TemporaryDirectory() as td:
            cfg = Path(td) / ".genericagent" / "hooks.json"
            cfg.parent.mkdir()
            cfg.write_text(json.dumps({"hooks": {"Stop": [{"hooks": [{
                "type": "prompt", "prompt": "x", "async": True,
            }]}]}}), encoding="utf-8")
            registry = load_hook_registry(config_dir=cfg.parent, home=Path(td) / "home")
            self.assertEqual(len(registry.entries), 0)
            self.assertTrue(registry.issues)


if __name__ == "__main__":
    unittest.main()
