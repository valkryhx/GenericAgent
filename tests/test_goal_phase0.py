"""Stage F phase 0: goal data model, /goal command, transcript events."""

import os
import tempfile
import unittest
from types import SimpleNamespace
from unittest import mock

from genericagent.core import goal as goal_core
from genericagent.core import transcripts as tx


class ParseGoalCommandTests(unittest.TestCase):
    def test_empty_is_status(self):
        self.assertEqual(goal_core.parse_goal_command("").kind, "status")
        self.assertEqual(goal_core.parse_goal_command("   ").kind, "status")

    def test_clear_words(self):
        for word in ["clear", "STOP", " off ", "Reset", "none", "cancel"]:
            self.assertEqual(goal_core.parse_goal_command(word).kind, "clear", word)

    def test_set_condition(self):
        cmd = goal_core.parse_goal_command("  all tests pass\r\nand lint clean  ")
        self.assertEqual(cmd.kind, "set")
        self.assertEqual(cmd.condition, "all tests pass\nand lint clean")

    def test_too_long_rejected(self):
        cmd = goal_core.parse_goal_command("x" * 4001)
        self.assertEqual(cmd.kind, "error")
        self.assertIn("4000", cmd.error)

    def test_custom_max_len(self):
        self.assertEqual(goal_core.parse_goal_command("x" * 501, max_len=500).kind, "error")
        self.assertEqual(goal_core.parse_goal_command("x" * 500, max_len=500).kind, "set")


class ActiveGoalTests(unittest.TestCase):
    def test_bumped(self):
        g = goal_core.ActiveGoal(condition="c")
        g2 = g.bumped("not yet")
        self.assertEqual(g.iterations, 0)
        self.assertEqual(g2.iterations, 1)
        self.assertEqual(g2.last_reason, "not yet")
        self.assertEqual(g2.condition, "c")


def _handler(output_schema=True, kind="prompt"):
    return SimpleNamespace(kind=kind, output_schema={"type": "object"} if output_schema else None)


def _agg(results):
    return SimpleNamespace(results=results)


class InterpretVerdictTests(unittest.TestCase):
    def test_ok_true_is_met(self):
        r = SimpleNamespace(handler=_handler(), status="success", output={"ok": True, "reason": "done"})
        st = goal_core.interpret_verdict(_agg([r]))
        self.assertTrue(st.met)
        self.assertFalse(st.failed)
        self.assertEqual(st.reason, "done")

    def test_ok_false_impossible_is_failed(self):
        r = SimpleNamespace(handler=_handler(), status="success",
                            output={"ok": False, "impossible": True, "reason": "impossible"})
        st = goal_core.interpret_verdict(_agg([r]))
        self.assertFalse(st.met)
        self.assertTrue(st.failed)
        self.assertTrue(st.impossible)

    def test_ok_false_is_not_met(self):
        r = SimpleNamespace(handler=_handler(), status="blocking",
                            output={"ok": False, "reason": "missing"})
        st = goal_core.interpret_verdict(_agg([r]))
        self.assertFalse(st.met)
        self.assertFalse(st.failed)
        self.assertIsNone(st.error_kind)

    def test_timeout(self):
        r = SimpleNamespace(handler=_handler(), status="timeout", output=None)
        st = goal_core.interpret_verdict(_agg([r]))
        self.assertFalse(st.met)
        self.assertEqual(st.error_kind, "evaluator_timeout")

    def test_invalid_output(self):
        r = SimpleNamespace(handler=_handler(), status="invalid_output", output=None)
        st = goal_core.interpret_verdict(_agg([r]))
        self.assertEqual(st.error_kind, "evaluator_error")

    def test_no_structured_evaluator(self):
        r = SimpleNamespace(handler=_handler(output_schema=False), status="success",
                            output={"ok": True})
        st = goal_core.interpret_verdict(_agg([r]))
        self.assertFalse(st.met)
        self.assertEqual(st.error_kind, "evaluator_error")

    def test_none_aggregate(self):
        self.assertEqual(goal_core.interpret_verdict(None).error_kind, "evaluator_error")


class TranscriptGoalEventsTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="ga_goal0_")
        self.path = tx.create_session(root=self.tmp, cwd=self.tmp, session_id="s_goal0")

    def tearDown(self):
        try:
            os.remove(self.path)
        except OSError:
            pass
        try:
            os.rmdir(self.tmp)
        except OSError:
            pass

    def test_record_goal_status_and_replay_ignores_it(self):
        tx.record_goal_status(self.path, session_id="s_goal0", met=False,
                              condition="c", reason="r", iterations=2)
        tx.record_active_goal(self.path, session_id="s_goal0",
                              goal=goal_core.ActiveGoal(condition="c"))
        loaded = tx.load_session(self.path)
        # goal_status / active_goal must not count as turns or messages,
        # and must not be reported as unsupported events.
        self.assertEqual(loaded.rounds, 0)
        self.assertFalse([w for w in loaded.warnings if "unsupported" in w])
        kinds = [e.get("type") for e in loaded.events]
        self.assertIn("goal_status", kinds)
        self.assertIn("active_goal", kinds)

    def test_sentinel_field(self):
        ev = tx.record_goal_status(self.path, session_id="s_goal0", met=False,
                                   condition="c", sentinel=True)
        self.assertTrue(ev["sentinel"])


class GoalCommandAppTests(unittest.TestCase):
    """Drive _handle_goal_command on a bare agent instance (no LLM)."""

    def _agent(self, active=None):
        from genericagent.core.app import GenericAgent
        a = GenericAgent.__new__(GenericAgent)
        a.active_goal = active
        a._queued_goal_origin = None
        a.session_path = None
        a.session_id = 's'
        a._record_goal_status = lambda **kw: None
        a._record_active_goal = lambda: None
        return a

    def _run(self, agent, arg):
        q = []
        agent._handle_goal_command(arg, _QueueLike(q))
        return q[-1]['done'] if q else None

    def test_set_status_clear_flow(self):
        a = self._agent()
        self.assertEqual(self._run(a, ""), "No goal set.")
        self.assertEqual(self._run(a, "make it pass"), "Goal set: make it pass")
        self.assertIn("Goal active: make it pass (0 turn", self._run(a, ""))
        self.assertEqual(self._run(a, "clear"), "Goal cleared: make it pass")
        self.assertEqual(self._run(a, ""), "No goal set.")

    def test_clear_without_goal(self):
        a = self._agent()
        self.assertEqual(self._run(a, "clear"), "No goal set.")

    def test_too_long_reports_error(self):
        a = self._agent()
        out = self._run(a, "y" * 4001)
        self.assertIn("4000", out)
        self.assertIsNone(a.active_goal)


class _QueueLike:
    def __init__(self, sink):
        self._sink = sink

    def put(self, item):
        self._sink.append(item)


if __name__ == "__main__":
    unittest.main()
