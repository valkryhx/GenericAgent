"""Stage F phase 1: goal Stop evaluator wiring + verdict application + phase filter."""

import json
import unittest
from types import SimpleNamespace
from unittest import mock

from genericagent.core import goal as goal_core
from genericagent.core import goal_evaluator as ge
from genericagent.hooks.config import HookRegistry
from genericagent.hooks.models import HookAggregate
from genericagent.hooks.runtime import HookRuntime


def _fake_runner(replies, calls=None):
    def run(kind, prompt, payload):
        if calls is not None:
            calls.append({"kind": kind, "prompt": prompt, "payload": payload})
        return replies.pop(0) if replies else json.dumps({"ok": False, "reason": "default"})
    return run


def _agent(**kw):
    a = SimpleNamespace(
        session_id="s", session_path=None, cwd=".", hook_runtime=None, active_goal=None,
        history=[], llmclient=None,
    )
    for k, v in kw.items():
        setattr(a, k, v)
    return a


class HandlerSpecTests(unittest.TestCase):
    def test_prompt_contains_condition_and_schema(self):
        spec = ge.build_goal_handler_spec("all tests pass")
        self.assertEqual(spec["type"], "prompt")
        self.assertIn("all tests pass", spec["prompt"])
        self.assertEqual(spec["outputSchema"]["required"], ["ok", "reason"])
        self.assertEqual(spec["onFailure"], "continue")

    def test_serialize_truncates_front(self):
        long_text = "\n".join("line-%d" % i for i in range(20000))
        out = ge.serialize_transcript(long_text, char_budget=1000)
        self.assertLessEqual(len(out), 1000 + 400)
        self.assertIn("truncated", out)

    def test_serialize_short_passthrough(self):
        self.assertEqual(ge.serialize_transcript([{"role": "user", "content": "hi"}]), "user: hi")


class RegisterHookTests(unittest.TestCase):
    def test_register_and_unregister(self):
        rt = HookRuntime(registry=HookRegistry(), prompt_runner=_fake_runner([]))
        a = _agent(hook_runtime=rt)
        key = ge.register_goal_hook(a, "cond")
        self.assertEqual(key, ge.GOAL_STOP_HOOK_KEY)
        hooks = rt.list_session_hooks("Stop")
        self.assertEqual(len(hooks), 1)
        self.assertEqual(hooks[0]["key"], "goal:stop")
        # idempotent replace, not stacking
        ge.register_goal_hook(a, "cond2")
        self.assertEqual(len(rt.list_session_hooks("Stop")), 1)
        self.assertTrue(ge.unregister_goal_hook(a))
        self.assertEqual(rt.list_session_hooks("Stop"), [])


class PhaseFilterTests(unittest.TestCase):
    def _runtime(self, calls):
        rt = HookRuntime(registry=HookRegistry(),
                         prompt_runner=_fake_runner([json.dumps({"ok": True, "reason": "y"})], calls))
        ge.register_goal_hook(_agent(hook_runtime=rt), "c")
        return rt

    def test_turn_end_runs_goal_hook(self):
        calls = []
        rt = self._runtime(calls)
        agg = rt.run_event(_agent(hook_runtime=rt), "Stop", {"phase": "turn_end"})
        self.assertEqual(len(calls), 1)
        self.assertIn("results", agg.__dict__)

    def test_user_turn_end_skips_goal_hook(self):
        calls = []
        rt = self._runtime(calls)
        rt.run_event(_agent(hook_runtime=rt), "Stop", {"phase": "user_turn_end"})
        self.assertEqual(calls, [])


class ApplyVerdictTests(unittest.TestCase):
    def _app_agent(self, active):
        from genericagent.core.app import GenericAgent
        a = GenericAgent.__new__(GenericAgent)
        a.active_goal = active
        a.hook_runtime = HookRuntime(registry=HookRegistry(), prompt_runner=_fake_runner([]))
        a.session_path = None
        a.session_id = "s"
        a._record_goal_status = lambda **kw: None
        a._record_active_goal = lambda: None
        return a

    def test_met_clears(self):
        a = self._app_agent(goal_core.ActiveGoal(condition="c"))
        ge.register_goal_hook(a, "c")
        a._apply_goal_verdict(goal_core.GoalStatus(met=True, reason="done"))
        self.assertIsNone(a.active_goal)
        self.assertEqual(a.hook_runtime.list_session_hooks("Stop"), [])

    def test_failed_clears(self):
        a = self._app_agent(goal_core.ActiveGoal(condition="c"))
        a._apply_goal_verdict(goal_core.GoalStatus(met=False, failed=True, impossible=True, reason="no"))
        self.assertIsNone(a.active_goal)

    def test_not_met_bumps(self):
        g = goal_core.ActiveGoal(condition="c")
        a = self._app_agent(g)
        a._apply_goal_verdict(goal_core.GoalStatus(met=False, reason="missing"))
        self.assertIs(a.active_goal, a.active_goal)
        self.assertEqual(a.active_goal.iterations, 1)
        self.assertEqual(a.active_goal.last_reason, "missing")

    def test_error_keeps_goal(self):
        g = goal_core.ActiveGoal(condition="c", iterations=2)
        a = self._app_agent(g)
        a._apply_goal_verdict(goal_core.GoalStatus(met=False, error_kind="evaluator_timeout"))
        self.assertEqual(a.active_goal.iterations, 2)


class GateIntegrationTests(unittest.TestCase):
    """Drive run_stop_gate end-to-end with a scripted evaluator aggregate."""

    def _make(self, aggregate):
        from genericagent.core.handler import GenericAgentHandler
        from genericagent.core.app import GenericAgent
        a = GenericAgent.__new__(GenericAgent)
        a.active_goal = goal_core.ActiveGoal(condition="c")
        a.is_subagent = False
        a.hook_runtime = object()  # non-None so gate is active
        a.session_path = None
        a.session_id = "s"
        a._record_goal_status = lambda **kw: None
        a._record_active_goal = lambda: None
        a._apply_goal_verdict = lambda st: GenericAgent._apply_goal_verdict(a, st)
        h = GenericAgentHandler.__new__(GenericAgentHandler)
        h.parent = a
        h.hook_parent_agent = None
        h.hook_runtime = a.hook_runtime
        h.permission_runtime = None
        h.last_hook_aggregate = None
        h._stop_gate_count = 0
        h.stop_emitted = False
        h.last_assistant_message = "assistant said x"
        return a, h

    def _patch_run(self, aggregate):
        return mock.patch("genericagent.hooks.runtime.run_hook_event", return_value=aggregate)

    def test_ok_true_ends_turn_and_clears(self):
        agg = HookAggregate(event="Stop")
        agg.decision = ""
        r = SimpleNamespace(handler=SimpleNamespace(kind="prompt", output_schema={"type": "object"}),
                            status="success", output={"ok": True, "reason": "done"})
        agg.results.append(r)
        a, h = self._make(agg)
        with self._patch_run(agg):
            action, text, got = h.run_stop_gate({})
        self.assertEqual(action, "none")
        self.assertIsNone(a.active_goal)

    def test_ok_false_blocks_and_bumps(self):
        agg = HookAggregate(event="Stop")
        agg.decision = "block"
        agg.reason = "missing"
        r = SimpleNamespace(handler=SimpleNamespace(kind="prompt", output_schema={"type": "object"}),
                            status="blocking", output={"ok": False, "reason": "missing"})
        agg.results.append(r)
        a, h = self._make(agg)
        with self._patch_run(agg):
            action, text, got = h.run_stop_gate({})
        self.assertEqual(action, "continue")
        self.assertIsNotNone(a.active_goal)
        self.assertEqual(a.active_goal.iterations, 1)


if __name__ == "__main__":
    unittest.main()

# --- iterations accounting on terminal verdict ---
class IterationsAccountingTests(unittest.TestCase):
    def _app_agent(self, active):
        from genericagent.core.app import GenericAgent
        a = GenericAgent.__new__(GenericAgent)
        a.active_goal = active
        a.hook_runtime = HookRuntime(registry=HookRegistry(), prompt_runner=_fake_runner([]))
        a.session_path = None
        a.session_id = "s"
        self.statuses = []
        a._record_goal_status = lambda **kw: self.statuses.append(kw)
        a._record_active_goal = lambda: None
        return a

    def test_met_records_iterations_plus_one(self):
        a = self._app_agent(goal_core.ActiveGoal(condition="c", iterations=3))
        a._apply_goal_verdict(goal_core.GoalStatus(met=True, reason="done"))
        terminal = [s for s in self.statuses if not s.get("sentinel")]
        self.assertTrue(terminal and terminal[-1]["met"])
        self.assertEqual(terminal[-1]["iterations"], 4)

    def test_not_met_bumps_by_one(self):
        a = self._app_agent(goal_core.ActiveGoal(condition="c", iterations=2))
        a._apply_goal_verdict(goal_core.GoalStatus(met=False, reason="no"))
        self.assertEqual(a.active_goal.iterations, 3)
