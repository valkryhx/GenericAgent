"""Stage F phase 5: goal metadata (S0), telemetry (S2), nudge (S3)."""

import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from genericagent.core import goal as goal_core
from genericagent.core import goal_metadata as gm
from genericagent.core import goal_telemetry as gt
from genericagent.core import goal_nudge as gn


class MetadataProjectionTests(unittest.TestCase):
    def test_none_is_null(self):
        self.assertIsNone(gm.goal_metadata(None))

    def test_core_fields(self):
        g = goal_core.ActiveGoal(condition="all tests pass", iterations=2, set_at=100.0, tokens_at_start=500)
        meta = gm.goal_metadata(g, current_tokens=1500)
        self.assertEqual(meta["condition"], "all tests pass")
        self.assertEqual(meta["iterations"], 2)
        self.assertEqual(meta["set_at"], 100.0)
        self.assertEqual(meta["tokens_at_start"], 500)
        self.assertEqual(meta["tokens_spent"], 1000)
        self.assertFalse(meta["met"])
        self.assertNotIn("last_reason", meta)

    def test_spent_never_negative(self):
        g = goal_core.ActiveGoal(condition="x", tokens_at_start=1000)
        self.assertEqual(gm.goal_metadata(g, current_tokens=200)["tokens_spent"], 0)

    def test_last_reason_included_when_set(self):
        g = goal_core.ActiveGoal(condition="x", last_reason="not yet")
        self.assertEqual(gm.goal_metadata(g)["last_reason"], "not yet")


class MetadataDiffTests(unittest.TestCase):
    def test_same_is_not_diff(self):
        a = {"condition": "x", "iterations": 1, "set_at": 1.0, "tokens_at_start": 0, "last_reason": None}
        b = dict(a)
        self.assertFalse(gm.differs(a, b))

    def test_token_tick_ignored(self):
        a = {"condition": "x", "iterations": 1, "set_at": 1.0, "tokens_at_start": 0, "tokens_spent": 0}
        b = dict(a, tokens_spent=999)
        self.assertFalse(gm.differs(a, b))

    def test_iteration_change_detected(self):
        a = {"condition": "x", "iterations": 1, "set_at": 1.0, "tokens_at_start": 0}
        self.assertTrue(gm.differs(a, dict(a, iterations=2)))

    def test_clear_detected(self):
        self.assertTrue(gm.differs({"condition": "x"}, None))
        self.assertTrue(gm.differs(None, {"condition": "x"}))
        self.assertFalse(gm.differs(None, None))


class TelemetryTests(unittest.TestCase):
    def test_default_local_writes_file(self):
        root = tempfile.mkdtemp()
        row = gt.record(gt.EVENT_SET, data_root=root, origin="user", iterations=0)
        self.assertIsNotNone(row)
        events = gt.read_events(root)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event"], "goal_set")
        self.assertEqual(events[0]["origin"], "user")

    def test_off_writes_nothing(self):
        root = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"GA_GOAL_TELEMETRY": "off"}):
            self.assertIsNone(gt.record(gt.EVENT_SET, data_root=root))
        self.assertEqual(gt.read_events(root), [])

    def test_no_condition_text_leaked(self):
        root = tempfile.mkdtemp()
        gt.record(gt.EVENT_ACHIEVED, data_root=root, reason="SECRET-CONDITION-TEXT", tokens=5)
        raw = (Path(root) / "logs" / "goal_events.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("SECRET-CONDITION-TEXT", raw)
        row = json.loads(raw.strip())
        self.assertEqual(row["reason_len"], len("SECRET-CONDITION-TEXT"))

    def test_best_effort_never_raises(self):
        # A bogus data_root that cannot be created must not raise.
        self.assertIsNone(gt.record(gt.EVENT_SET, data_root="\x00bad"))


class NudgeTests(unittest.TestCase):
    def test_disabled_by_default(self):
        env = dict(os.environ); env.pop("GA_GOAL_NUDGE", None)
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertFalse(gn.nudge_enabled())

    def test_enabled_values(self):
        for v in ("1", "true", "ON", "yes"):
            with mock.patch.dict(os.environ, {"GA_GOAL_NUDGE": v}):
                self.assertTrue(gn.nudge_enabled(), v)
        for v in ("0", "false", "off", ""):
            with mock.patch.dict(os.environ, {"GA_GOAL_NUDGE": v}):
                self.assertFalse(gn.nudge_enabled(), v)

    def test_gate(self):
        self.assertFalse(gn.should_nudge(enabled=False, has_active_goal=False, sessions_since_last=99))
        self.assertFalse(gn.should_nudge(enabled=True, has_active_goal=True, sessions_since_last=99))
        self.assertFalse(gn.should_nudge(enabled=True, has_active_goal=False, sessions_since_last=1, cooldown_sessions=3))
        self.assertTrue(gn.should_nudge(enabled=True, has_active_goal=False, sessions_since_last=3, cooldown_sessions=3))
        self.assertFalse(gn.should_nudge(enabled=True, has_active_goal=False, sessions_since_last=99, relevant=False))

    def test_cooldown_advances_and_fires_once(self):
        root = tempfile.mkdtemp()
        with mock.patch.dict(os.environ, {"GA_GOAL_NUDGE": "1", "GA_GOAL_NUDGE_COOLDOWN_SESSIONS": "3"}):
            results = [gn.evaluate(root, has_active_goal=False) for _ in range(4)]
        # sessions 1,2 -> None; session 3 -> text; session 4 -> None (counter reset)
        self.assertEqual(results[0], None)
        self.assertEqual(results[1], None)
        self.assertIsNotNone(results[2])
        self.assertEqual(results[3], None)

    def test_default_off_never_writes_state(self):
        root = tempfile.mkdtemp()
        env = {k: v for k, v in os.environ.items() if k != "GA_GOAL_NUDGE"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertIsNone(gn.evaluate(root))
        self.assertFalse((Path(root) / "goal_nudge_state.json").exists())


class AppGoalWiringTests(unittest.TestCase):
    """Drive the real app.py goal state points with a stubbed agent."""

    def _agent(self, root):
        from genericagent.core.app import GenericAgent
        a = GenericAgent.__new__(GenericAgent)
        a.active_goal = None
        a.session_path = None
        a.session_id = "s"
        a.data_root = root
        a._last_goal_metadata = None
        a.events = []
        a.hook_emit = lambda ev: a.events.append(ev)
        a._record_active_goal = lambda: None
        return a

    def test_set_emits_metadata_and_telemetry(self):
        root = tempfile.mkdtemp()
        a = self._agent(root)
        with mock.patch.object(goal_evaluator, "register_goal_hook", lambda *x, **k: None):
            a._set_goal("all tests pass")
        metas = [e for e in a.events if e.get("type") == "goal_metadata"]
        self.assertEqual(len(metas), 1)
        self.assertEqual(metas[0]["goal"]["condition"], "all tests pass")
        self.assertFalse(metas[0]["goal"]["met"])
        events = gt.read_events(root)
        self.assertEqual([e["event"] for e in events], ["goal_set"])

    def test_token_tick_does_not_repush(self):
        root = tempfile.mkdtemp()
        a = self._agent(root)
        with mock.patch.object(goal_evaluator, "register_goal_hook", lambda *x, **k: None):
            a._set_goal("x")
        before = len([e for e in a.events if e.get("type") == "goal_metadata"])
        a._notify_goal_metadata()  # same state -> must be deduped
        after = len([e for e in a.events if e.get("type") == "goal_metadata"])
        self.assertEqual(before, after)

    def test_met_verdict_pushes_terminal_and_clears(self):
        root = tempfile.mkdtemp()
        a = self._agent(root)
        with mock.patch.object(goal_evaluator, "register_goal_hook", lambda *x, **k: None):
            a._set_goal("x")
        with mock.patch.object(goal_evaluator, "unregister_goal_hook", lambda *x, **k: None):
            status = goal_core.GoalStatus(met=True, reason="done")
            a._apply_goal_verdict(status)
        self.assertIsNone(a.active_goal)
        metas = [e for e in a.events if e.get("type") == "goal_metadata"]
        last = metas[-1]
        self.assertIsNone(last["goal"])
        self.assertTrue(last["terminal"]["met"])
        names = [e["event"] for e in gt.read_events(root)]
        self.assertIn("goal_met", names)
        self.assertIn("goal_achieved", names)

    def test_error_verdict_keeps_goal_and_records(self):
        root = tempfile.mkdtemp()
        a = self._agent(root)
        with mock.patch.object(goal_evaluator, "register_goal_hook", lambda *x, **k: None):
            a._set_goal("x")
        status = goal_core.GoalStatus(met=False, error_kind="evaluator_timeout")
        a._apply_goal_verdict(status)
        self.assertIsNotNone(a.active_goal)
        events = gt.read_events(root)
        met = [e for e in events if e["event"] == "goal_met"]
        self.assertTrue(met and met[-1]["outcome"] == "evaluator_timeout")


from genericagent.core import goal_evaluator as goal_evaluator  # noqa: E402


class BridgeNudgeWiringTests(unittest.TestCase):
    """The bridge emits goal_nudge only when enabled and due."""

    def _bridge(self, root):
        from frontends.ink import bridge as bridge_mod
        b = bridge_mod.GenericAgentBridge.__new__(bridge_mod.GenericAgentBridge)
        b.agent = SimpleNamespace(active_goal=None, data_root=root)
        b.events = []
        b.emit = lambda ev: b.events.append(ev)
        return b

    def test_bridge_emits_nudge_when_enabled(self):
        root = tempfile.mkdtemp()
        b = self._bridge(root)
        with mock.patch.dict(os.environ, {"GA_GOAL_NUDGE": "1", "GA_GOAL_NUDGE_COOLDOWN_SESSIONS": "1"}):
            b.emit_goal_nudge_if_any()
        nudges = [e for e in b.events if e.get("type") == "goal_nudge"]
        self.assertEqual(len(nudges), 1)
        self.assertIn("/goal", nudges[0]["text"])

    def test_bridge_silent_when_disabled(self):
        root = tempfile.mkdtemp()
        b = self._bridge(root)
        env = {k: v for k, v in os.environ.items() if k != "GA_GOAL_NUDGE"}
        with mock.patch.dict(os.environ, env, clear=True):
            b.emit_goal_nudge_if_any()
        self.assertEqual([e for e in b.events if e.get("type") == "goal_nudge"], [])


class CumulativeTokenTests(unittest.TestCase):
    """Session-level cumulative OUTPUT token counter (mirrors CC Th())."""

    def test_record_usage_accumulates_output(self):
        from genericagent.llm import core as llm_core
        sess = SimpleNamespace(last_usage_tokens=None, session_output_tokens=0)
        llm_core._record_usage({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}, "chat_completions", sess)
        self.assertEqual(sess.session_output_tokens, 5)
        llm_core._record_usage({"prompt_tokens": 20, "completion_tokens": 7, "total_tokens": 27}, "chat_completions", sess)
        self.assertEqual(sess.session_output_tokens, 12)

    def test_record_usage_handles_missing_counter_attr(self):
        from genericagent.llm import core as llm_core
        sess = SimpleNamespace(last_usage_tokens=None)  # no session_output_tokens yet
        llm_core._record_usage({"prompt_tokens": 1, "completion_tokens": 4, "total_tokens": 5}, "chat_completions", sess)
        self.assertEqual(sess.session_output_tokens, 4)

    def test_token_position_prefers_cumulative_output(self):
        from genericagent.core.app import GenericAgent
        a = GenericAgent.__new__(GenericAgent)
        a.llmclient = SimpleNamespace(backend=SimpleNamespace(session_output_tokens=1234, last_usage_tokens={"output_tokens": 9}))
        self.assertEqual(a._goal_token_position(), 1234)

    def test_token_position_falls_back_to_last_output(self):
        from genericagent.core.app import GenericAgent
        a = GenericAgent.__new__(GenericAgent)
        a.llmclient = SimpleNamespace(backend=SimpleNamespace(last_usage_tokens={"output_tokens": 42}))
        self.assertEqual(a._goal_token_position(), 42)

    def test_goal_tokens_spent_uses_cumulative_delta(self):
        root = tempfile.mkdtemp()
        from genericagent.core.app import GenericAgent
        a = GenericAgent.__new__(GenericAgent)
        a.active_goal = None
        a.session_path = None
        a.session_id = "s"
        a.data_root = root
        a._last_goal_metadata = None
        a.hook_emit = lambda ev: None
        a._record_active_goal = lambda: None
        backend = SimpleNamespace(session_output_tokens=100)
        a.llmclient = SimpleNamespace(backend=backend)
        with mock.patch.object(goal_evaluator, "register_goal_hook", lambda *x, **k: None):
            a._set_goal("x")
        self.assertEqual(a.active_goal.tokens_at_start, 100)
        # another 350 output tokens spent while the goal was active
        backend.session_output_tokens = 450
        with mock.patch.object(goal_evaluator, "unregister_goal_hook", lambda *x, **k: None):
            a._clear_goal(met=True, reason="done", iterations=1)
        row = [e for e in gt.read_events(root) if e["event"] == "goal_achieved"][-1]
        self.assertEqual(row["tokens"], 350)


if __name__ == "__main__":
    unittest.main()
