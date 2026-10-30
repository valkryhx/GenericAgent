"""Phase 6 gaps 3/4/5: superseded telemetry, evaluator retry, metadata snapshot."""

import json
import tempfile
import unittest

from genericagent.core import goal as goal_core
from genericagent.hooks.config import HookRegistry
from genericagent.hooks.models import HookHandler, HookResult
from genericagent.hooks.runner import _prompt_execute
from genericagent.hooks.runtime import HookRuntime


def _base_agent(**kw):
    from genericagent.core.app import GenericAgent
    a = GenericAgent.__new__(GenericAgent)
    a.active_goal = None
    a.hook_runtime = HookRuntime(registry=HookRegistry(), prompt_runner=lambda *x: "")
    a.session_path = None
    a.session_id = "s"
    a.hook_emit = None
    a._last_goal_metadata = None
    a._record_goal_status = lambda **k: None
    a._record_active_goal = lambda: None
    a._goal_token_position = lambda: 500
    for k, v in kw.items():
        setattr(a, k, v)
    return a


def _result(handler):
    return HookResult(
        event="Stop", handler=handler, source="session",
        source_path="session:hook", hook_run_id="run", status="running")


SCHEMA = {
    "type": "object",
    "required": ["ok", "reason"],
    "properties": {"ok": {"type": "boolean"}, "reason": {"type": "string"}},
}


class SupersedeTests(unittest.TestCase):
    def test_overwrite_records_superseded_telemetry(self):
        from genericagent.core import goal_telemetry as gt
        with tempfile.TemporaryDirectory() as tmp:
            a = _base_agent(data_root=tmp)
            a.active_goal = goal_core.ActiveGoal(condition="old", tokens_at_start=100)
            a._set_goal("new")
            self.assertEqual("new", a.active_goal.condition)
            rows = gt.read_events(tmp)
            cleared = [r for r in rows if r["event"] == gt.EVENT_CLEARED]
            self.assertTrue(cleared)
            self.assertEqual("superseded", cleared[-1].get("outcome"))
            sets = [r for r in rows if r["event"] == gt.EVENT_SET]
            self.assertTrue(sets)

    def test_user_clear_marks_user_cleared(self):
        from genericagent.core import goal_telemetry as gt
        with tempfile.TemporaryDirectory() as tmp:
            a = _base_agent(data_root=tmp)
            a.active_goal = goal_core.ActiveGoal(condition="c", tokens_at_start=0)
            a._clear_goal(met=False, reason="cleared by user", outcome="user_cleared")
            rows = gt.read_events(tmp)
            cleared = [r for r in rows if r["event"] == gt.EVENT_CLEARED]
            self.assertEqual("user_cleared", cleared[-1].get("outcome"))


class EvaluatorRetryTests(unittest.TestCase):
    def _handler(self, retry):
        opts = {"type": "prompt", "prompt": "judge", "outputSchema": SCHEMA, "timeout": 5}
        if retry:
            opts["retryOnInvalidOutput"] = True
        return HookHandler(kind="prompt", options=opts)

    def test_retry_turns_prose_into_success(self):
        calls = []
        replies = ["Sure! here is my answer: looks good", json.dumps({"ok": True, "reason": "done"})]

        def runner(kind, prompt, payload):
            calls.append(prompt)
            return replies.pop(0)

        h = self._handler(True)
        result = _result(h)
        _prompt_execute(h, {}, result, runner)
        self.assertEqual(result.status, "success")
        self.assertEqual(result.output["ok"], True)
        self.assertEqual(len(calls), 2)
        self.assertIn("ONLY a single JSON object", calls[1])

    def test_no_retry_when_flag_absent(self):
        calls = []

        def runner(kind, prompt, payload):
            calls.append(prompt)
            return "nope, not json"

        h = self._handler(False)
        result = _result(h)
        _prompt_execute(h, {}, result, runner)
        self.assertEqual(result.status, "invalid_output")
        self.assertEqual(len(calls), 1)

    def test_both_invalid_stays_invalid(self):
        calls = []

        def runner(kind, prompt, payload):
            calls.append(prompt)
            return "still not json"

        h = self._handler(True)
        result = _result(h)
        _prompt_execute(h, {}, result, runner)
        self.assertEqual(result.status, "invalid_output")
        self.assertEqual(len(calls), 2)

    def test_goal_spec_opts_into_retry(self):
        from genericagent.core import goal_evaluator as ge
        spec = ge.build_goal_handler_spec("x")
        self.assertTrue(spec.get("retryOnInvalidOutput"))


class MetadataSnapshotTests(unittest.TestCase):
    def test_force_emits_even_when_unchanged(self):
        emitted = []
        a = _base_agent(hook_emit=emitted.append)
        a.active_goal = goal_core.ActiveGoal(condition="keep going", tokens_at_start=0)
        a._notify_goal_metadata()
        self.assertEqual(len(emitted), 1)
        a._notify_goal_metadata()
        self.assertEqual(len(emitted), 1)
        a._notify_goal_metadata(force=True)
        self.assertEqual(len(emitted), 2)
        self.assertEqual(emitted[-1]["type"], "goal_metadata")
        self.assertEqual(emitted[-1]["goal"]["condition"], "keep going")

    def test_pending_terminal_badge_flushed_once(self):
        emitted = []
        a = _base_agent(hook_emit=emitted.append)
        a._pending_terminal_goal = {"met": True, "condition": "done", "iterations": 1, "tokens": 5}
        a._notify_goal_metadata(force=True)
        self.assertEqual(emitted[-1].get("terminal", {}).get("met"), True)
        self.assertIsNone(a._pending_terminal_goal)
        a._notify_goal_metadata(force=True)
        self.assertNotIn("terminal", emitted[-1])


if __name__ == "__main__":
    unittest.main()

class ActiveGoalLineTests(unittest.TestCase):
    def test_none_and_empty(self):
        from genericagent.core.goal_metadata import active_goal_line
        self.assertEqual(active_goal_line(None), "")
        self.assertEqual(active_goal_line(goal_core.ActiveGoal(condition="")), "")

    def test_single_and_plural(self):
        from genericagent.core.goal_metadata import active_goal_line
        self.assertEqual(active_goal_line(goal_core.ActiveGoal(condition="x", iterations=1)), "Goal: x (1 turn)")
        self.assertEqual(active_goal_line(goal_core.ActiveGoal(condition="x", iterations=2)), "Goal: x (2 turns)")
