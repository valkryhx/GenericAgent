import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import session_transcript
from frontends import continue_cmd


class FakeBackend:
    def __init__(self):
        self.history = []


class FakeClient:
    def __init__(self):
        self.backend = FakeBackend()
        self.last_tools = "cached"


class FakeAgent:
    def __init__(self, log_path):
        self.log_path = str(log_path)
        self.history = ["old"]
        self.handler = object()
        self.llmclient = FakeClient()
        self.llmclients = [self.llmclient]
        self.aborted = False

    def abort(self):
        self.aborted = True


def write_native_log(path, user_text="hello", assistant_text="hi"):
    prompt = {
        "role": "user",
        "content": [{"type": "text", "text": user_text}],
    }
    response = [{"type": "text", "text": assistant_text}]
    Path(path).write_text(
        "=== Prompt === 2026-05-23 13:00:00\n"
        + json.dumps(prompt, ensure_ascii=False, indent=2)
        + "\n\n=== Response === 2026-05-23 13:00:01\n"
        + repr(response)
        + "\n\n",
        encoding="utf-8",
    )


class ContinueCmdResumeTest(unittest.TestCase):
    def test_list_sessions_excludes_actual_agent_log_path_not_only_pid_name(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            current = log_dir / "model_responses_123456.txt"
            old = log_dir / "model_responses_999999.txt"
            write_native_log(current, "current")
            write_native_log(old, "old")
            with (
                patch.object(continue_cmd, "_LOG_GLOB", str(log_dir / "model_responses_*.txt")),
                patch.object(continue_cmd, "_SESSION_ROOT", str(log_dir / "empty_sessions")),
            ):
                sessions = continue_cmd.list_sessions(exclude_path=str(current))
            self.assertEqual([str(old)], [item[0] for item in sessions])

    def test_reset_conversation_snapshots_actual_agent_log_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp)
            current = log_dir / "model_responses_123456.txt"
            write_native_log(current)
            agent = FakeAgent(current)
            with patch.object(continue_cmd, "_LOG_DIR", str(log_dir)):
                message = continue_cmd.reset_conversation(agent)
            snapshots = list(log_dir.glob("model_responses_snapshot_*"))
            self.assertEqual("🆕 已开启新对话，当前上下文已清空", message)
            self.assertEqual(1, len(snapshots))
            self.assertEqual("", current.read_text(encoding="utf-8"))
            self.assertEqual([], agent.llmclient.backend.history)
            self.assertEqual("", agent.llmclient.last_tools)

    def test_legacy_extract_ui_messages_keeps_tool_result_with_assistant_turn(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "model_responses_111111.txt"
            first_prompt = {"role": "user", "content": [{"type": "text", "text": "search"}]}
            first_response = [
                {"type": "text", "text": "I will search"},
                {"type": "tool_use", "id": "tool-1", "name": "web_search", "input": {"q": "x"}},
            ]
            second_prompt = {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "tool-1", "content": "result text"},
                    {"type": "text", "text": "### [WORKING MEMORY]\n<history></history>"},
                ],
            }
            second_response = [{"type": "text", "text": "done"}]
            path.write_text(
                "=== Prompt === 2026-05-23 13:00:00\n"
                + json.dumps(first_prompt, ensure_ascii=False, indent=2)
                + "\n\n=== Response === 2026-05-23 13:00:01\n"
                + repr(first_response)
                + "\n\n=== Prompt === 2026-05-23 13:00:02\n"
                + json.dumps(second_prompt, ensure_ascii=False, indent=2)
                + "\n\n=== Response === 2026-05-23 13:00:03\n"
                + repr(second_response)
                + "\n\n",
                encoding="utf-8",
            )

            messages = continue_cmd.extract_ui_messages(str(path))

            self.assertEqual("user", messages[0]["role"])
            self.assertEqual("search", messages[0]["content"])
            self.assertEqual("assistant", messages[1]["role"])
            self.assertIn("Tool: `web_search`", messages[1]["content"])
            self.assertIn("result text", messages[1]["content"])
            self.assertIn("LLM Running (Turn 2)", messages[1]["content"])
            self.assertEqual(2, len(messages))

    def test_list_sessions_includes_transcripts_before_legacy_logs(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            transcript_root = root / "sessions"
            log_dir = root / "model_responses"
            log_dir.mkdir()
            transcript = session_transcript.create_session(
                root=transcript_root,
                cwd="C:/repo",
                session_id="session_test",
            )
            session_transcript.record_turn(
                transcript,
                session_id="session_test",
                turn_id=1,
                source="user",
                user_text="from transcript",
                assistant_text="answer",
                backend_history_before=[],
                backend_history_after=[{"role": "user", "content": "from transcript"}],
            )
            legacy = log_dir / "model_responses_111111.txt"
            write_native_log(legacy, "from legacy")

            with (
                patch.object(continue_cmd, "_LOG_GLOB", str(log_dir / "model_responses_*.txt")),
                patch.object(continue_cmd, "_SESSION_ROOT", str(transcript_root)),
            ):
                sessions = continue_cmd.list_sessions()

            self.assertEqual(str(transcript), sessions[0][0])
            self.assertEqual("from transcript", sessions[0][2])
            self.assertEqual(str(legacy), sessions[1][0])

    def test_restore_dispatches_transcript_path_to_session_transcript(self):
        with tempfile.TemporaryDirectory() as tmp:
            transcript = session_transcript.create_session(root=tmp, cwd="C:/repo", session_id="session_test")
            history = [{"role": "user", "content": "hello"}]
            session_transcript.record_turn(
                transcript,
                session_id="session_test",
                turn_id=1,
                source="user",
                user_text="hello",
                assistant_text="hi",
                backend_history_before=[],
                backend_history_after=history,
            )
            agent = FakeAgent(Path(tmp) / "model_responses_123456.txt")

            message, ok = continue_cmd.restore(agent, str(transcript))

            self.assertTrue(ok)
            self.assertIn("结构化会话", message)
            self.assertEqual(history, agent.llmclient.backend.history)


if __name__ == "__main__":
    unittest.main()
