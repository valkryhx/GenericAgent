"""Phase 6 gap 1: goal state survives resume (active_goal + hook + tokens)."""

import json
import tempfile
import unittest
from pathlib import Path

from genericagent.core import transcripts as session_transcript
from genericagent.core import goal as goal_core


class FakeBackend:
    def __init__(self):
        self.history = []
        self.session_output_tokens = 0
        self.last_usage_tokens = None


class FakeClient:
    def __init__(self):
        self.backend = FakeBackend()
        self.last_tools = ""


class FakeAgent:
    def __init__(self):
        self.history = []
        self.handler = None
        self.llmclient = FakeClient()
        self.active_goal = None
        self.hook_runtime = None
        self.hook_emit = None

    def abort(self):
        pass


def _new_path(tmp, sid="session_gr"):
    return session_transcript.create_session(root=tmp, cwd="C:/repo", session_id=sid)


class GoalResumeTests(unittest.TestCase):
    def test_record_active_goal_persists_tokens_at_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _new_path(tmp)
            g = goal_core.ActiveGoal(condition="all green", tokens_at_start=1234, iterations=2)
            session_transcript.record_active_goal(path, session_id="session_gr", goal=g)
            loaded = session_transcript.load_session(path)
            self.assertIsNotNone(loaded.active_goal)
            self.assertEqual("all green", loaded.active_goal["condition"])
            self.assertEqual(1234, loaded.active_goal["tokens_at_start"])
            self.assertEqual(2, loaded.active_goal["iterations"])

    def test_latest_active_goal_wins_and_clear_records_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _new_path(tmp)
            session_transcript.record_active_goal(
                path, session_id="session_gr",
                goal=goal_core.ActiveGoal(condition="first"))
            session_transcript.record_active_goal(
                path, session_id="session_gr",
                goal=goal_core.ActiveGoal(condition="second"))
            loaded = session_transcript.load_session(path)
            self.assertEqual("second", loaded.active_goal["condition"])
            # A later clear (goal=None) wipes the active goal.
            session_transcript.record_active_goal(path, session_id="session_gr", goal=None)
            loaded2 = session_transcript.load_session(path)
            self.assertIsNone(loaded2.active_goal)

    def test_terminal_goal_status_recorded_as_badge(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _new_path(tmp)
            session_transcript.record_goal_status(
                path, session_id="session_gr", met=True, condition="done",
                reason="evidence", iterations=3, tokens=42)
            loaded = session_transcript.load_session(path)
            self.assertEqual("done", loaded.terminal_goal["condition"])
            self.assertTrue(loaded.terminal_goal["met"])
            self.assertEqual(42, loaded.terminal_goal["tokens"])

    def test_restore_rebuilds_active_goal_and_hook(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _new_path(tmp)
            backend_tokens = 5000
            session_transcript.record_active_goal(
                path, session_id="session_gr",
                goal=goal_core.ActiveGoal(
                    condition="ship it", tokens_at_start=2000, iterations=4, origin="user"))
            session_transcript.append_working_state(
                path, session_id="session_gr", handler_working=None,
                last_tools="", last_usage_tokens=None,
                session_output_tokens=backend_tokens)
            agent = FakeAgent()
            result = session_transcript.restore_agent_session(agent, path)
            self.assertTrue(result.ok)
            self.assertIsNotNone(agent.active_goal)
            self.assertEqual("ship it", agent.active_goal.condition)
            self.assertEqual(2000, agent.active_goal.tokens_at_start)
            self.assertEqual(4, agent.active_goal.iterations)
            # session cumulative output tokens are restored so tokens_spent is stable
            self.assertEqual(backend_tokens, agent.llmclient.backend.session_output_tokens)
            # the goal:stop hook is re-registered so the gate works again
            runtime = agent.hook_runtime
            self.assertIsNotNone(runtime)
            keys = [entry.get("key") for entry in runtime.list_session_hooks("Stop")]
            self.assertIn("goal:stop", keys)

    def test_restore_with_only_terminal_goal_does_not_rearm_hook(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _new_path(tmp)
            session_transcript.record_active_goal(
                path, session_id="session_gr",
                goal=goal_core.ActiveGoal(condition="x"))
            session_transcript.record_active_goal(path, session_id="session_gr", goal=None)
            session_transcript.record_goal_status(
                path, session_id="session_gr", met=True, condition="x",
                reason="ok", iterations=2, tokens=10)
            agent = FakeAgent()
            session_transcript.restore_agent_session(agent, path)
            self.assertIsNone(agent.active_goal)
            self.assertEqual("x", agent._pending_terminal_goal["condition"])
            runtime = agent.hook_runtime
            # No goal hook should be armed when there is no active goal.
            hooks = runtime.list_session_hooks("Stop") if runtime is not None else []
            self.assertNotIn("goal:stop", [e.get("key") for e in hooks])


if __name__ == "__main__":
    unittest.main()
