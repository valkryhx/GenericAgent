"""Stage 2-A: defer goal evaluation while background sub-agents are in flight."""

import json
import unittest
from types import SimpleNamespace
from unittest import mock

from genericagent.core import goal as goal_core
from genericagent.core import goal_evaluator as ge
from genericagent.hooks.config import HookRegistry
from genericagent.hooks.models import HookAggregate
from genericagent.hooks.runtime import HookRuntime


def _state(**kw):
    base = dict(turn_status="running", process_status="alive")
    base.update(kw)
    return SimpleNamespace(**base)


class InFlightStateTests(unittest.TestCase):
    def test_running_alive_is_in_flight(self):
        self.assertTrue(ge.is_in_flight_agent_state(_state()))

    def test_pending_alive_is_in_flight(self):
        self.assertTrue(ge.is_in_flight_agent_state(_state(turn_status="pending")))

    def test_exited_is_not(self):
        self.assertFalse(ge.is_in_flight_agent_state(_state(turn_status="running", process_status="exited")))

    def test_shutdown_killed_not(self):
        for ps in ("shutdown", "killed"):
            self.assertFalse(ge.is_in_flight_agent_state(_state(process_status=ps)))

    def test_has_in_flight_agents_via_manager(self):
        class Mgr:
            def __init__(self, states): self._s = states
            def list_descendants(self, sid): return self._s
        a = SimpleNamespace(session_id="s", _get_subagent_manager=lambda: Mgr([_state()]))
        self.assertTrue(ge.has_in_flight_agents(a))
        a2 = SimpleNamespace(session_id="s", _get_subagent_manager=lambda: Mgr([]))
        self.assertFalse(ge.has_in_flight_agents(a2))

    def test_has_in_flight_no_session(self):
        a = SimpleNamespace(session_id="", _get_subagent_manager=lambda: (_ for _ in ()).throw(RuntimeError()))
        self.assertFalse(ge.has_in_flight_agents(a))

    def test_manager_on_handler_when_agent_lacks_factory(self):
        # Production shape: run_stop_gate has the GenericAgent (no manager
        # factory) plus the handler (factory + session). This is the exact bug
        # where has_in_flight_agents used to read the agent only and dead-code.
        class Mgr:
            def __init__(self, states): self._s = states
            def list_descendants(self, sid): return self._s
        agent = SimpleNamespace(session_id="s")
        handler = SimpleNamespace(parent=SimpleNamespace(session_id="s"),
                                  _get_subagent_manager=lambda: Mgr([_state()]))
        self.assertTrue(ge.has_in_flight_agents(agent, handler))
        handler_idle = SimpleNamespace(parent=SimpleNamespace(session_id="s"),
                                       _get_subagent_manager=lambda: Mgr([_state(process_status="exited")]))
        self.assertFalse(ge.has_in_flight_agents(agent, handler_idle))

    def test_manager_on_handler_uses_parent_session_when_agent_unset(self):
        class Mgr:
            def __init__(self, states): self._s = states
            def list_descendants(self, sid):
                assert sid == "parent-sid"
                return self._s
        agent = SimpleNamespace(session_id="")
        handler = SimpleNamespace(parent=SimpleNamespace(session_id="parent-sid"),
                                  _get_subagent_manager=lambda: Mgr([_state()]))
        self.assertTrue(ge.has_in_flight_agents(agent, handler))


class DeferGateTests(unittest.TestCase):
    def _make(self, in_flight):
        from genericagent.core.app import GenericAgent
        from genericagent.core.handler import GenericAgentHandler
        a = GenericAgent.__new__(GenericAgent)
        a.active_goal = goal_core.ActiveGoal(condition="c")
        a.is_subagent = False
        a.hook_runtime = HookRuntime(registry=HookRegistry(), prompt_runner=lambda *x: json.dumps({"ok": True}))
        a.session_path = None
        a.session_id = "s"
        a.task_dir = None; a.verbose = False; a.stop_sig = False
        a.history = []
        self.deferred = []
        a._record_goal_deferred = lambda: self.deferred.append(1)
        a._apply_goal_verdict = lambda st: (_ for _ in ()).throw(AssertionError("should not evaluate when deferred"))
        h = GenericAgentHandler(a, [], ".", workspace_root=None)
        h.hook_runtime = a.hook_runtime
        h.permission_runtime = None
        h.max_turns = 6
        return a, h

    def test_in_flight_defers_and_does_not_block(self):
        a, h = self._make(True)
        with mock.patch("genericagent.core.goal_evaluator.has_in_flight_agents", return_value=True):
            action, text, agg = h.run_stop_gate({})
        self.assertEqual(action, "none")
        self.assertEqual(len(self.deferred), 1)
        self.assertIsNotNone(a.active_goal)  # kept, not cleared

    def test_not_in_flight_evaluates_normally(self):
        a, h = self._make(False)
        agg = HookAggregate(event="Stop")
        r = SimpleNamespace(handler=SimpleNamespace(kind="prompt", output_schema={"type": "object"}),
                            status="success", output={"ok": True, "reason": "done"})
        agg.results.append(r)
        # replace the throw-guard with a real apply
        a._apply_goal_verdict = lambda st: None
        with mock.patch("genericagent.core.goal_evaluator.has_in_flight_agents", return_value=False), \
             mock.patch("genericagent.hooks.runtime.run_hook_event", return_value=agg):
            action, text, got = h.run_stop_gate({})
        self.assertEqual(action, "none")
        self.assertEqual(len(self.deferred), 0)


class RuntimeSkipOnDeferTests(unittest.TestCase):
    def test_goal_hook_skipped_when_goal_defer(self):
        calls = []
        def runner(kind, prompt, payload):
            calls.append(prompt)
            return json.dumps({"ok": False, "reason": "x"})
        rt = HookRuntime(registry=HookRegistry(), prompt_runner=runner)
        a = SimpleNamespace(session_id="s", session_path=None, cwd=".", hook_runtime=rt,
                            history=[], llmclient=None, active_goal=None)
        ge.register_goal_hook(a, "c")
        rt.run_event(a, "Stop", {"phase": "turn_end", "goal_defer": True})
        self.assertEqual(calls, [])
        rt.run_event(a, "Stop", {"phase": "turn_end"})
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
