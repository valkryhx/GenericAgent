"""Stage F phase 4: propose_goal tool, gates, and approval runtime."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from genericagent.core import goal as goal_core
from genericagent.core import goal_proposal as gp


def _settings_file(obj):
    d = tempfile.mkdtemp()
    p = Path(d) / "settings.json"
    p.write_text(json.dumps(obj), encoding="utf-8")
    return p


class NormalizeTests(unittest.TestCase):
    def test_blank(self):
        self.assertFalse(gp.normalize_proposal_condition("   ").ok)

    def test_clear_word(self):
        for w in ("clear", "STOP", "reset"):
            r = gp.normalize_proposal_condition(w)
            self.assertFalse(r.ok, w)

    def test_ok_and_trim(self):
        r = gp.normalize_proposal_condition("  all tests pass  ")
        self.assertTrue(r.ok)
        self.assertEqual(r.condition, "all tests pass")

    def test_separators_collapse(self):
        r = gp.normalize_proposal_condition("a\r\nb\u2028c")
        self.assertTrue(r.ok)

    def test_too_long(self):
        self.assertFalse(gp.normalize_proposal_condition("x" * 501).ok)
        self.assertTrue(gp.normalize_proposal_condition("x" * 500).ok)


class ResolveAskUserTests(unittest.TestCase):
    def test_always_ask_forces(self):
        self.assertTrue(gp.resolve_ask_user(gp.SETTING_ALWAYS_ASK, False))

    def test_auto_default_asks(self):
        self.assertTrue(gp.resolve_ask_user(gp.SETTING_AUTO, None))

    def test_auto_explicit_false(self):
        self.assertFalse(gp.resolve_ask_user(gp.SETTING_AUTO, False))


class SettingTests(unittest.TestCase):
    def test_auto_default_when_missing(self):
        self.assertEqual(gp.read_model_proposed_goals_setting(settings_path=Path("/nonexistent/x.json")), gp.SETTING_AUTO)

    def test_values(self):
        self.assertEqual(gp.read_model_proposed_goals_setting(settings_path=_settings_file({"modelProposedGoals": "disabled"})), gp.SETTING_DISABLED)
        self.assertEqual(gp.read_model_proposed_goals_setting(settings_path=_settings_file({"model_proposed_goals": "alwaysAsk"})), gp.SETTING_ALWAYS_ASK)

    def test_unknown_value_is_auto(self):
        self.assertEqual(gp.read_model_proposed_goals_setting(settings_path=_settings_file({"modelProposedGoals": "weird"})), gp.SETTING_AUTO)

    def test_feature_off_by_default(self):
        # Default OFF: each propose_goal pops an approval dialog, so an
        # auto-proposing model would nag the user; opt-in only.
        env = dict(os.environ)
        env.pop("GA_PROPOSE_GOAL_ENABLED", None)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(gp.is_feature_enabled())

    def test_feature_on_when_enabled(self):
        for v in ("1", "true", "yes", "on", "ON"):
            with mock.patch.dict(os.environ, {"GA_PROPOSE_GOAL_ENABLED": v}):
                self.assertTrue(gp.is_feature_enabled(), v)
        for v in ("0", "false", "no", "off", ""):
            with mock.patch.dict(os.environ, {"GA_PROPOSE_GOAL_ENABLED": v}):
                self.assertFalse(gp.is_feature_enabled(), v)


class RuntimeTests(unittest.TestCase):
    def test_headless_denies(self):
        rt = gp.GoalProposalRuntime()
        self.assertFalse(rt.request_approval("c", timeout=1.0))

    def test_resolve_approve(self):
        rt = gp.GoalProposalRuntime()
        events = []
        rt.set_emit(lambda e: events.append(e))
        import threading
        result = {}
        def worker():
            result["ok"] = rt.request_approval("c", timeout=5.0)
        th = threading.Thread(target=worker); th.start()
        # wait until a request id is emitted
        import time
        end = time.time() + 3
        rid = None
        while time.time() < end:
            reqs = [e for e in events if e.get("type") == "goal_proposal_request"]
            if reqs:
                rid = reqs[0]["requestId"]; break
            time.sleep(0.02)
        self.assertIsNotNone(rid)
        self.assertTrue(rt.resolve(rid, "approve"))
        th.join(timeout=5)
        self.assertTrue(result.get("ok"))

    def test_resolve_reject(self):
        rt = gp.GoalProposalRuntime()
        events = []
        rt.set_emit(lambda e: events.append(e))
        import threading, time
        result = {}
        th = threading.Thread(target=lambda: result.__setitem__("ok", rt.request_approval("c", timeout=5.0)))
        th.start()
        rid = None; end = time.time() + 3
        while time.time() < end:
            reqs = [e for e in events if e.get("type") == "goal_proposal_request"]
            if reqs: rid = reqs[0]["requestId"]; break
            time.sleep(0.02)
        rt.resolve(rid, "reject")
        th.join(timeout=5)
        self.assertFalse(result.get("ok"))


class GatedToolTests(unittest.TestCase):
    def _handler(self, agent):
        from genericagent.core.handler import GenericAgentHandler
        h = GenericAgentHandler(agent, [], ".", workspace_root=None)
        h.permission_runtime = None
        return h

    def _agent(self, **over):
        a = SimpleNamespace(
            is_subagent=False,
            handler=None,
            goal_proposal_runtime=gp.GoalProposalRuntime(),
            _queued_goal_origin=None,
            queued=[],
            applied=[],
        )
        a.put_task = lambda q, source=None: a.queued.append((q, source))
        a._goal_proposal_gate = lambda: None
        a._apply_goal_proposal = lambda cond, ask_user=True, origin=None: a.applied.append((cond, ask_user)) or "queued"
        for k, v in over.items():
            setattr(a, k, v)
        return a

    def test_subagent_blocked(self):
        a = self._agent(is_subagent=True)
        a._goal_proposal_gate = lambda: "propose_goal is only available in the main session, not in a subagent"
        h = self._handler(a)
        out = h.do_propose_goal({"condition": "x"}, None)
        self.assertEqual(out.data.get("reason"), "goal_proposal_rejected")

    def test_invalid_condition(self):
        a = self._agent()
        h = self._handler(a)
        out = h.do_propose_goal({"condition": "   "}, None)
        self.assertEqual(out.data.get("reason"), "invalid_condition")

    def test_ok_direct(self):
        a = self._agent()
        h = self._handler(a)
        with mock.patch.object(gp, "read_model_proposed_goals_setting", return_value=gp.SETTING_AUTO):
            out = h.do_propose_goal({"condition": "all tests pass", "ask_user": False}, None)
        self.assertEqual(out.data.get("status"), "ok")
        self.assertFalse(out.data.get("askUser"))
        self.assertEqual(a.applied, [("all tests pass", False)])

    def test_ok_default_asks(self):
        a = self._agent()
        h = self._handler(a)
        with mock.patch.object(gp, "read_model_proposed_goals_setting", return_value=gp.SETTING_AUTO):
            out = h.do_propose_goal({"condition": "diff clean"}, None)
        self.assertTrue(out.data.get("askUser"))
        self.assertEqual(a.applied, [("diff clean", True)])

    def test_reentrancy_blocked(self):
        a = self._agent()
        # simulate an in-flight proposal
        rt = a.goal_proposal_runtime
        rt._pending["abc"] = mock.MagicMock(done=lambda: False)
        h = self._handler(a)
        out = h.do_propose_goal({"condition": "x"}, None)
        self.assertEqual(out.data.get("reason"), "proposal_in_flight")


class ApplyProposalTests(unittest.TestCase):
    """Real GenericAgent._apply_goal_proposal / _goal_proposal_gate logic."""

    def _agent(self):
        from genericagent.core.app import GenericAgent
        a = GenericAgent.__new__(GenericAgent)
        a.is_subagent = False
        a.handler = None
        a.goal_proposal_runtime = gp.GoalProposalRuntime()
        a._queued_goal_origin = None
        a.active_goal = None
        a.queued = []
        a.put_task = lambda q, source=None: a.queued.append((q, source))
        return a

    def test_direct_queues_goal_with_direct_origin(self):
        a = self._agent()
        msg = a._apply_goal_proposal("all green", ask_user=False)
        self.assertIn("direct", msg.lower())
        self.assertEqual(a.queued, [("/goal all green", "goal_proposal")])
        self.assertEqual(a._queued_goal_origin, goal_core.ORIGIN_PROPOSAL_DIRECT)

    def test_ask_without_ui_does_not_queue(self):
        a = self._agent()  # runtime has no emit
        msg = a._apply_goal_proposal("all green", ask_user=True)
        self.assertIn("could not be shown", msg)
        self.assertEqual(a.queued, [])

    def test_gate_subagent(self):
        a = self._agent(); a.is_subagent = True
        self.assertIsNotNone(a._goal_proposal_gate())

    def test_gate_feature_off(self):
        a = self._agent()
        with mock.patch.object(gp, "is_feature_enabled", return_value=False):
            self.assertIsNotNone(a._goal_proposal_gate())

    def test_gate_plan_mode(self):
        a = self._agent()
        a.handler = SimpleNamespace(_in_plan_mode=lambda: "/tmp/plan.md")
        with mock.patch.object(gp, "is_feature_enabled", return_value=True),              mock.patch.object(gp, "read_model_proposed_goals_setting", return_value=gp.SETTING_AUTO):
            self.assertIn("plan mode", (a._goal_proposal_gate() or ""))

    def test_gate_disabled_setting(self):
        a = self._agent()
        with mock.patch.object(gp, "is_feature_enabled", return_value=True),              mock.patch.object(gp, "read_model_proposed_goals_setting", return_value=gp.SETTING_DISABLED):
            self.assertIsNotNone(a._goal_proposal_gate())

    def test_gate_allows_when_clear(self):
        a = self._agent()
        with mock.patch.object(gp, "read_model_proposed_goals_setting", return_value=gp.SETTING_AUTO), \
             mock.patch.object(gp, "is_feature_enabled", return_value=True):
            self.assertIsNone(a._goal_proposal_gate())


class CapabilityTests(unittest.TestCase):
    def test_generic_subagent_cannot_see_propose_goal(self):
        from genericagent.agents.capabilities import build_subagent_capability_profile
        prof = build_subagent_capability_profile(
            is_subagent=True, available_tools=["file_read", "spawn_agent", "propose_goal", "ask_user"],
        )
        self.assertFalse(prof.allows("propose_goal"))

    def test_main_agent_keeps_propose_goal(self):
        from genericagent.agents.capabilities import build_subagent_capability_profile
        prof = build_subagent_capability_profile(
            is_subagent=False, available_tools=["file_read", "propose_goal", "ask_user"],
        )
        self.assertTrue(prof.allows("propose_goal"))


if __name__ == "__main__":
    unittest.main()
