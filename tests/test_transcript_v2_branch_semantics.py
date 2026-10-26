import json
import tempfile
import unittest
from pathlib import Path

from genericagent.core import transcripts as session_transcript


class FakeBackend:
    def __init__(self):
        self.history = []


class FakeClient:
    def __init__(self):
        self.backend = FakeBackend()
        self.last_tools = ""


class FakeAgent:
    def __init__(self):
        self.history = []
        self.handler = None
        self.session_id = ""
        self.session_path = None
        self.session_turn_id = 0
        self.llmclient = FakeClient()

    def abort(self):
        pass


def make_v1(root, session_id, history):
    path = session_transcript.create_session(
        root=root, cwd="C:/repo", session_id=session_id, schema=1
    )
    session_transcript.record_turn(
        path,
        session_id=session_id,
        turn_id=1,
        source="user",
        user_text="old question",
        assistant_text="old answer",
        backend_history_before=[],
        backend_history_after=history,
    )
    return path


class TranscriptV2BranchSemanticsTest(unittest.TestCase):
    def test_writer_marks_tool_result_origin_from_blocks(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = session_transcript.create_session(
                root=tmp, cwd="C:/repo", session_id="session_tool_origin"
            )
            agent = FakeAgent()
            agent.session_id = "session_tool_origin"
            agent.session_path = path
            agent.llmclient.backend.history = [
                {"role": "user", "content": [{"type": "text", "text": "read it"}]},
                {"role": "assistant", "content": [
                    {"type": "tool_use", "id": "call_1", "name": "file_read", "input": {}},
                ]},
                {"role": "user", "content": [
                    {"type": "tool_result", "tool_use_id": "call_1", "content": "body"},
                ]},
                {"role": "assistant", "content": [{"type": "text", "text": "done"}]},
            ]
            session_transcript.record_agent_turn(
                agent,
                user_text="read it",
                assistant_text="done",
                source="user",
                backend_history_before=[],
            )
            events = [
                json.loads(line)
                for line in Path(path).read_text(encoding="utf-8").splitlines()
            ]
            messages = [event for event in events if event["type"] == "message"]
            self.assertEqual(
                ["human", "assistant", "tool_result", "assistant"],
                [event["origin"]["kind"] for event in messages],
            )
            loaded = session_transcript.load_session(path)
            self.assertEqual(["read it"], [turn.user_text for turn in loaded.turns])
            self.assertEqual(
                [
                    {"role": "user", "content": "read it"},
                    {"role": "assistant", "content": "done"},
                ],
                loaded.ui_messages,
            )

    def test_reloading_v2_branch_reconstructs_its_v1_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            history = [
                {"role": "user", "content": [{"type": "text", "text": "old question"}]},
                {"role": "assistant", "content": [{"type": "text", "text": "old answer"}]},
            ]
            legacy = make_v1(tmp, "session_branch_source", history)
            agent = FakeAgent()
            session_transcript.restore_agent_session(agent, legacy)
            agent.llmclient.backend.history = history + [
                {"role": "user", "content": [{"type": "text", "text": "new question"}]},
                {"role": "assistant", "content": [{"type": "text", "text": "new answer"}]},
            ]
            session_transcript.record_agent_turn(
                agent,
                user_text="new question",
                assistant_text="new answer",
                source="user",
                backend_history_before=history,
            )
            branch_path = agent.session_path

            loaded = session_transcript.load_session(branch_path)
            self.assertEqual(
                ["old question", "new question"],
                [turn.user_text for turn in loaded.turns],
            )
            self.assertEqual([1, 2], [turn.turn_id for turn in loaded.turns])
            self.assertEqual(history + [
                {"role": "user", "content": [{"type": "text", "text": "new question"}]},
                {"role": "assistant", "content": [{"type": "text", "text": "new answer"}]},
            ], loaded.backend_history)

            branch_summary = next(
                item for item in session_transcript.list_sessions(root=tmp)
                if item.path == branch_path
            )
            self.assertEqual(2, branch_summary.rounds)
            self.assertEqual("old question", branch_summary.preview)

            restored_again = FakeAgent()
            session_transcript.restore_agent_session(restored_again, branch_path)
            self.assertEqual(1, restored_again.session_turn_id)
            self.assertEqual(4, len(restored_again.llmclient.backend.history))

    def test_compact_listing_matches_full_load_without_changing_compact_semantics(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = session_transcript.create_session(
                root=tmp, cwd="C:/repo", session_id="session_compact_listing"
            )
            agent = FakeAgent()
            agent.session_id = "session_compact_listing"
            agent.session_path = path
            first = [
                {"role": "user", "content": [{"type": "text", "text": "one"}]},
                {"role": "assistant", "content": [{"type": "text", "text": "a1"}]},
            ]
            agent.llmclient.backend.history = first
            session_transcript.record_agent_turn(
                agent, user_text="one", assistant_text="a1", source="user",
                backend_history_before=[],
            )
            session_transcript.record_compact(
                path,
                session_id="session_compact_listing",
                message="summary",
                backend_history_after=first,
            )
            agent.llmclient.backend.history = first + [
                {"role": "user", "content": [{"type": "text", "text": "after compact"}]},
                {"role": "assistant", "content": [{"type": "text", "text": "after answer"}]},
            ]
            session_transcript.record_agent_turn(
                agent, user_text="after compact", assistant_text="after answer",
                source="user", backend_history_before=len(first),
            )
            loaded = session_transcript.load_session(path)
            summary = session_transcript.list_sessions(root=tmp)[0]
            self.assertEqual(loaded.rounds, summary.rounds)
            self.assertEqual(loaded.preview, summary.preview)
            self.assertEqual(
                [turn.user_text for turn in loaded.turns if turn.user_text],
                summary.user_texts,
            )

    def test_version_detection_stops_at_the_first_valid_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "session_probe.jsonl"
            path.write_text(
                '{"version":2,"type":"session_start","session_id":"s"}\n'
                + ('{"version":2,"type":"message"}\n' * 100000),
                encoding="utf-8",
                newline="\n",
            )
            self.assertEqual(2, session_transcript._detect_session_version(path))

    def test_workflow_events_are_ignored_by_v2_rounds_and_projection(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = session_transcript.create_session(
                root=tmp, cwd="C:/repo", session_id="session_workflow_events"
            )
            session_transcript.append_message(
                path, session_id="session_workflow_events", role="user",
                blocks=[{"type": "text", "text": "run workflow"}],
                origin={"kind": "human"}, turn_id=1, text="run workflow",
            )
            session_transcript.append_message(
                path, session_id="session_workflow_events", role="assistant",
                blocks=[{"type": "text", "text": "done"}],
                origin={"kind": "assistant"}, turn_id=1, text="done",
            )
            session_transcript.append_turn_end(
                path, session_id="session_workflow_events", turn_id=1,
                first_uuid="first", last_uuid="last",
            )
            session_transcript.record_workflow_event(
                path, session_id="session_workflow_events", run_id="wf_1",
                event_type="workflow_started", artifact_dir="artifacts/wf_1",
            )
            session_transcript.record_workflow_event(
                path, session_id="session_workflow_events", run_id="wf_1",
                event_type="workflow_completed", artifact_dir="artifacts/wf_1",
                result_ref="artifacts/wf_1/result.json",
            )
            loaded = session_transcript.load_session(path)
            summary = session_transcript.list_sessions(root=tmp)[0]
            self.assertEqual(1, loaded.rounds)
            self.assertEqual(1, summary.rounds)
            self.assertEqual("run workflow", summary.preview)
            self.assertEqual([], loaded.warnings)
            self.assertEqual(
                ["workflow_started", "workflow_completed"],
                [event["type"] for event in loaded.events if event["type"].startswith("workflow_")],
            )


if __name__ == "__main__":
    unittest.main()
