import json
import tempfile
import unittest
from pathlib import Path

import session_transcript


class FakeBackend:
    def __init__(self):
        self.history = []


class FakeClient:
    def __init__(self):
        self.backend = FakeBackend()
        self.last_tools = "cached tools"


class FakeAgent:
    def __init__(self):
        self.history = ["old ui history"]
        self.handler = object()
        self.llmclient = FakeClient()
        self.llmclients = [self.llmclient]
        self.aborted = False

    def abort(self):
        self.aborted = True


class SessionTranscriptTest(unittest.TestCase):
    def test_create_session_writes_session_start_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = session_transcript.create_session(root=tmp, cwd="C:/repo")

            lines = Path(path).read_text(encoding="utf-8").splitlines()
            self.assertEqual(1, len(lines))
            event = json.loads(lines[0])
            self.assertEqual(1, event["version"])
            self.assertEqual("session_start", event["type"])
            self.assertTrue(event["session_id"].startswith("session_"))
            self.assertEqual("C:/repo", event["cwd"])

    def test_record_turn_and_load_session_round_trips_ui_and_backend_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = session_transcript.create_session(root=tmp, cwd="C:/repo", session_id="session_test")
            before = []
            after = [
                {"role": "user", "content": [{"type": "text", "text": "hello"}]},
                {"role": "assistant", "content": [{"type": "text", "text": "hi"}]},
            ]

            session_transcript.record_turn(
                path,
                session_id="session_test",
                turn_id=1,
                source="user",
                user_text="hello",
                assistant_text="hi",
                backend_history_before=before,
                backend_history_after=after,
            )

            loaded = session_transcript.load_session(path)
            self.assertEqual("session_test", loaded.session_id)
            self.assertEqual(1, loaded.rounds)
            self.assertEqual("hello", loaded.preview)
            self.assertEqual(
                [
                    {"role": "user", "content": "hello"},
                    {"role": "assistant", "content": "hi"},
                ],
                loaded.ui_messages,
            )
            self.assertEqual(after, loaded.backend_history)
            self.assertEqual(before, loaded.turns[0].backend_history_before)

    def test_list_sessions_returns_newest_first_and_skips_malformed_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = session_transcript.create_session(root=tmp, cwd="C:/repo", session_id="session_a")
            second = session_transcript.create_session(root=tmp, cwd="C:/repo", session_id="session_b")
            Path(tmp, "broken.jsonl").write_text("{not json", encoding="utf-8")
            session_transcript.record_turn(
                second,
                session_id="session_b",
                turn_id=1,
                source="user",
                user_text="newer",
                assistant_text="answer",
                backend_history_before=[],
                backend_history_after=[{"role": "user", "content": "newer"}],
            )

            sessions = session_transcript.list_sessions(root=tmp, include_empty=True)

            self.assertEqual(["session_b", "session_a"], [s.session_id for s in sessions])
            self.assertEqual(str(second), sessions[0].path)
            self.assertEqual(str(first), sessions[1].path)

    def test_list_sessions_skips_empty_session_start_files_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            empty = session_transcript.create_session(root=tmp, cwd="C:/repo", session_id="session_empty")
            full = session_transcript.create_session(root=tmp, cwd="C:/repo", session_id="session_full")
            session_transcript.record_turn(
                full,
                session_id="session_full",
                turn_id=1,
                source="user",
                user_text="hello",
                assistant_text="hi",
                backend_history_before=[],
                backend_history_after=[{"role": "user", "content": "hello"}],
            )

            sessions = session_transcript.list_sessions(root=tmp)

            self.assertEqual(["session_full"], [s.session_id for s in sessions])
            self.assertEqual(str(full), sessions[0].path)
            self.assertNotIn(str(empty), [s.path for s in sessions])

    def test_restore_session_replaces_backend_history_and_clears_runtime_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = session_transcript.create_session(root=tmp, cwd="C:/repo", session_id="session_test")
            history = [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
            session_transcript.record_turn(
                path,
                session_id="session_test",
                turn_id=1,
                source="user",
                user_text="hello",
                assistant_text="hi",
                backend_history_before=[],
                backend_history_after=history,
            )
            agent = FakeAgent()

            result = session_transcript.restore_agent_session(agent, path)

            self.assertTrue(result.ok)
            self.assertTrue(agent.aborted)
            self.assertEqual(history, agent.llmclient.backend.history)
            self.assertEqual("", agent.llmclient.last_tools)
            self.assertIsNone(agent.handler)
            self.assertEqual("session_test", agent.session_id)
            self.assertEqual(str(path), agent.session_path)
            self.assertEqual(1, agent.session_turn_id)

    def test_restored_session_continues_in_same_transcript_with_next_turn_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = session_transcript.create_session(root=tmp, cwd="C:/repo", session_id="session_test")
            first_after = [{"role": "user", "content": "one"}]
            second_after = first_after + [{"role": "assistant", "content": "two"}]
            session_transcript.record_turn(
                path,
                session_id="session_test",
                turn_id=1,
                source="user",
                user_text="one",
                assistant_text="a1",
                backend_history_before=[],
                backend_history_after=first_after,
            )
            session_transcript.record_turn(
                path,
                session_id="session_test",
                turn_id=2,
                source="user",
                user_text="two",
                assistant_text="a2",
                backend_history_before=first_after,
                backend_history_after=second_after,
            )
            agent = FakeAgent()

            session_transcript.restore_agent_session(agent, path)
            agent.llmclient.backend.history = second_after + [{"role": "user", "content": "three"}]
            session_transcript.record_agent_turn(
                agent,
                user_text="three",
                assistant_text="a3",
                source="user",
                backend_history_before=second_after,
            )

            loaded = session_transcript.load_session(path)
            self.assertEqual(str(path), agent.session_path)
            self.assertEqual("session_test", agent.session_id)
            self.assertEqual(3, agent.session_turn_id)
            self.assertEqual([1, 2, 3], [turn.turn_id for turn in loaded.turns])
            self.assertEqual(["one", "two", "three"], [turn.user_text for turn in loaded.turns])

    def test_record_agent_turn_uses_agent_session_and_increments_turn_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = session_transcript.create_session(root=tmp, cwd="C:/repo", session_id="session_test")
            agent = FakeAgent()
            agent.session_id = "session_test"
            agent.session_path = str(path)
            agent.session_turn_id = 0
            before = []
            after = [{"role": "user", "content": [{"type": "text", "text": "hello"}]}]
            agent.llmclient.backend.history = after

            session_transcript.record_agent_turn(
                agent,
                user_text="hello",
                assistant_text="hi",
                source="user",
                backend_history_before=before,
            )

            loaded = session_transcript.load_session(path)
            self.assertEqual(1, agent.session_turn_id)
            self.assertEqual(1, loaded.rounds)
            self.assertEqual(after, loaded.backend_history)
            self.assertEqual("hello", loaded.ui_messages[0]["content"])
            self.assertEqual("hi", loaded.ui_messages[1]["content"])


if __name__ == "__main__":
    unittest.main()
