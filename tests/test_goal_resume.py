"""Phase 6 / CC-aligned: goal survives resume (re-armed) + clear/supersede sentinel."""

import tempfile
import unittest

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


class PendingConditionTests(unittest.TestCase):
    def test_record_active_goal_persists_tokens_at_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _new_path(tmp)
            g = goal_core.ActiveGoal(condition="all green", tokens_at_start=1234, iterations=2)
            session_transcript.record_active_goal(path, session_id="session_gr", goal=g)
            loaded = session_transcript.load_session(path)
            self.assertEqual("all green", loaded.active_goal["condition"])
            self.assertEqual(1234, loaded.active_goal["tokens_at_start"])

    def test_pending_from_latest_active_goal_when_no_goal_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _new_path(tmp)
            session_transcript.record_active_goal(
                path, session_id="session_gr", goal=goal_core.ActiveGoal(condition="ship"))
            loaded = session_transcript.load_session(path)
            self.assertEqual("ship", loaded.pending_goal_condition)

    def test_clear_sentinel_resolves_pending(self):
        """A user clear writes goal_status met:true sentinel -> no pending on resume."""
        with tempfile.TemporaryDirectory() as tmp:
            path = _new_path(tmp)
            session_transcript.record_active_goal(
                path, session_id="session_gr", goal=goal_core.ActiveGoal(condition="ship"))
            # What _clear_goal(user) writes now: met:true sentinel.
            session_transcript.record_goal_status(
                path, session_id="session_gr", met=True, condition="ship",
                reason="cleared by user", sentinel=True)
            session_transcript.record_active_goal(path, session_id="session_gr", goal=None)
            loaded = session_transcript.load_session(path)
            self.assertIsNone(loaded.pending_goal_condition)
            self.assertIsNone(loaded.terminal_goal)  # sentinel never becomes a badge

    def test_real_met_status_is_terminal_and_not_pending(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _new_path(tmp)
            session_transcript.record_goal_status(
                path, session_id="session_gr", met=True, condition="done",
                reason="evidence", iterations=3, tokens=42)
            loaded = session_transcript.load_session(path)
            self.assertIsNone(loaded.pending_goal_condition)
            self.assertEqual("done", loaded.terminal_goal["condition"])
            self.assertTrue(loaded.terminal_goal["met"])


class RestoreTests(unittest.TestCase):
    def test_restore_rearms_fresh_like_cc(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _new_path(tmp)
            session_transcript.record_active_goal(
                path, session_id="session_gr",
                goal=goal_core.ActiveGoal(
                    condition="ship it", tokens_at_start=2000, iterations=4, origin="user"))
            session_transcript.append_working_state(
                path, session_id="session_gr", handler_working=None,
                last_tools="", last_usage_tokens=None,
                session_output_tokens=5000)
            agent = FakeAgent()
            result = session_transcript.restore_agent_session(agent, path)
            self.assertTrue(result.ok)
            g = agent.active_goal
            self.assertIsNotNone(g)
            self.assertEqual("ship it", g.condition)
            self.assertEqual(0, g.iterations)              # re-armed, not 4
            self.assertEqual(5000, g.tokens_at_start)      # current cumulative
            self.assertEqual(goal_core.ORIGIN_RESTORED, g.origin)
            self.assertEqual(5000, agent.llmclient.backend.session_output_tokens)
            keys = [e.get("key") for e in agent.hook_runtime.list_session_hooks("Stop")]
            self.assertIn("goal:stop", keys)

    def test_restore_skips_when_hooks_disabled(self):
        import os
        from unittest import mock
        with tempfile.TemporaryDirectory() as tmp:
            path = _new_path(tmp)
            session_transcript.record_active_goal(
                path, session_id="session_gr", goal=goal_core.ActiveGoal(condition="x"))
            agent = FakeAgent()
            with mock.patch.dict(os.environ, {"GA_HOOKS_DISABLED": "1"}):
                session_transcript.restore_agent_session(agent, path)
            self.assertIsNone(agent.active_goal)

    def test_restore_with_only_terminal_goal_does_not_rearm_hook(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = _new_path(tmp)
            session_transcript.record_active_goal(
                path, session_id="session_gr", goal=goal_core.ActiveGoal(condition="x"))
            session_transcript.record_goal_status(
                path, session_id="session_gr", met=True, condition="x",
                reason="ok", iterations=2, tokens=10)
            session_transcript.record_active_goal(path, session_id="session_gr", goal=None)
            agent = FakeAgent()
            session_transcript.restore_agent_session(agent, path)
            self.assertIsNone(agent.active_goal)
            self.assertEqual("x", agent._pending_terminal_goal["condition"])
            hooks = agent.hook_runtime.list_session_hooks("Stop") if agent.hook_runtime else []
            self.assertNotIn("goal:stop", [e.get("key") for e in hooks])


if __name__ == "__main__":
    unittest.main()
