"""Stage E: high-value event emission (Notification / MessageDisplay / UserPromptExpansion).

Each event has a real emission point with the CC payload fields, and is
observational: hooks can add context / alter display / suppress, but a hook
error never breaks the flow.
"""
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from genericagent.core.app import GenericAgent
from genericagent.core.handler import GenericAgentHandler
from genericagent.hooks.models import HookAggregate, HookHandler, HookResult


def _recorder(sink, *, extra=None):
    def record(agent, event, data=None, *, emit=None):
        sink.append((event, dict(data or {})))
        agg = HookAggregate(event=event)
        if extra and event in extra:
            handler = HookHandler("command", {"command": "test"})
            agg.add(HookResult(event, handler, "test", "test", "h", "success",
                               output={"hookSpecificOutput": extra[event]}))
        return agg
    return record


class UserPromptExpansionTests(unittest.TestCase):
    def _agent(self):
        a = GenericAgent.__new__(GenericAgent)
        a.hook_emit = None
        return a

    def test_resume_expansion_fires_event(self):
        a = self._agent()
        calls = []
        with mock.patch("genericagent.hooks.runtime.run_hook_event", side_effect=_recorder(calls)):
            out = a._expand_prompt("slash_command", "/resume", "", "the prompt")
        self.assertEqual(calls[0][0], "UserPromptExpansion")
        self.assertEqual(calls[0][1]["expansion_type"], "slash_command")
        self.assertEqual(calls[0][1]["command_name"], "/resume")
        self.assertEqual(out, "the prompt")

    def test_expansion_context_appended(self):
        a = self._agent()
        def record(agent, event, data=None, *, emit=None):
            agg = HookAggregate(event=event)
            agg.additional_context.append("extra ctx")
            return agg
        with mock.patch("genericagent.hooks.runtime.run_hook_event", side_effect=record):
            out = a._expand_prompt("slash_command", "/x", "", "base")
        self.assertIn("base", out)
        self.assertIn("extra ctx", out)

    def test_expansion_suppress_returns_none(self):
        a = self._agent()
        extra = {"UserPromptExpansion": {"hookEventName": "UserPromptExpansion", "suppressOriginalPrompt": True}}
        with mock.patch("genericagent.hooks.runtime.run_hook_event", side_effect=_recorder([], extra=extra)):
            out = a._expand_prompt("slash_command", "/x", "", "base")
        self.assertIsNone(out)


class MessageDisplayTests(unittest.TestCase):
    def _agent(self):
        a = GenericAgent.__new__(GenericAgent)
        a.hook_emit = None
        a.session_turn_id = 3
        return a

    def test_message_display_can_override(self):
        a = self._agent()
        handler = SimpleNamespace(current_turn=2)
        calls = []
        extra = {"MessageDisplay": {"hookEventName": "MessageDisplay", "displayContent": "SHOWN"}}
        with mock.patch("genericagent.hooks.runtime.run_hook_event", side_effect=_recorder(calls, extra=extra)):
            shown = a._emit_message_display(handler, "original text")
        self.assertEqual(shown, "SHOWN")
        self.assertEqual(calls[0][0], "MessageDisplay")
        self.assertTrue(calls[0][1]["final"])
        self.assertEqual(calls[0][1]["delta"], "original text")

    def test_message_display_default_is_unchanged(self):
        a = self._agent()
        handler = SimpleNamespace(current_turn=1)
        with mock.patch("genericagent.hooks.runtime.run_hook_event", side_effect=_recorder([])):
            shown = a._emit_message_display(handler, "original")
        self.assertEqual(shown, "original")


class NotificationTests(unittest.TestCase):
    def test_notify_fires_with_payload(self):
        parent = SimpleNamespace(task_dir=None, verbose=False, hook_runtime=object(), session_id="s", cwd=".")
        h = GenericAgentHandler(parent, cwd=".", workspace_root=None)
        calls = []
        with mock.patch("genericagent.hooks.runtime.run_hook_event", side_effect=_recorder(calls)):
            h._notify("permission_request", "need approval for code_run", title="Permission required")
        self.assertEqual(calls[0][0], "Notification")
        self.assertEqual(calls[0][1]["notification_type"], "permission_request")
        self.assertEqual(calls[0][1]["message"], "need approval for code_run")
        self.assertEqual(calls[0][1]["title"], "Permission required")

    def test_notify_swallows_errors(self):
        parent = SimpleNamespace(task_dir=None, verbose=False, hook_runtime=object(), session_id="s", cwd=".")
        h = GenericAgentHandler(parent, cwd=".", workspace_root=None)
        with mock.patch("genericagent.hooks.runtime.run_hook_event", side_effect=RuntimeError("boom")):
            h._notify("permission_request", "x")  # must not raise


class EventStatusTests(unittest.TestCase):
    def test_promoted_events_report_integrated(self):
        from genericagent.hooks.runtime import HookRuntime
        from genericagent.hooks.config import HookRegistry
        from genericagent.hooks.audit import HookAudit
        import tempfile
        rt = HookRuntime(registry=HookRegistry(), audit=HookAudit(Path(tempfile.mkdtemp()) / "a"))
        status = rt.event_status()
        for ev in ("Notification", "MessageDisplay", "UserPromptExpansion", "SessionEnd",
                   "PreModelSwitch", "PostModelSwitch"):
            self.assertEqual(status[ev], "integrated", ev)


if __name__ == "__main__":
    unittest.main()
