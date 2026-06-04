import copy
import io
import json
import os
import subprocess
import queue
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTENDS = REPO_ROOT / "frontends"
if str(FRONTENDS) not in sys.path:
    sys.path.insert(0, str(FRONTENDS))


from ink_bridge import GenericAgentBridge, encode_event, make_stdout_emitter, run_jsonl_loop  # noqa: E402


class FakeBackend:
    def __init__(self):
        self.history = []
        self.last_usage_tokens = None


class FakeClient:
    def __init__(self):
        self.backend = FakeBackend()
        self.last_tools = ""


class FakeAgent:
    def __init__(self):
        self.inc_out = False
        self.verbose = True
        self.is_running = False
        self.aborted = False
        self.prompts = []
        self.queues = []
        self.history = []
        self.handler = object()
        self.llmclient = FakeClient()
        self.llmclients = [self.llmclient]
        self.llm_no = 0

    def run(self):
        return None

    def put_task(self, text, source="user"):
        self.prompts.append((text, source))
        self.history.append(f"[USER]: {text}")
        self.llmclient.backend.history.append({"role": "user", "content": text})
        dq = queue.Queue()
        self.queues.append(dq)
        return dq

    def abort(self):
        self.aborted = True

    def list_llms(self):
        return [
            (0, "NativeOAISession/gpt-native", self.llm_no == 0),
            (1, "NativeOAISession/kimi-native", self.llm_no == 1),
        ]

    def select_llm(self, selector):
        if str(selector).lower() in {"1", "kimi"}:
            self.llm_no = 1
            return {"ok": True, "index": 1, "name": "NativeOAISession/kimi-native", "model": "moonshotai/kimi-k2.6"}
        return {"ok": False, "code": "not_found", "message": "model not found"}


class InkBridgeTest(unittest.TestCase):
    def test_encode_event_writes_compact_json_line(self):
        line = encode_event({"type": "assistant_delta", "text": "你好\nworld"})

        self.assertEqual({"type": "assistant_delta", "text": "你好\nworld"}, json.loads(line))
        self.assertTrue(line.endswith("\n"))

    def test_encode_event_escapes_non_ascii_for_pipe_transport(self):
        line = encode_event({"type": "assistant_delta", "text": "公益token暂停"})

        self.assertNotIn("公益", line)
        self.assertIn("\\u", line)
        self.assertEqual({"type": "assistant_delta", "text": "公益token暂停"}, json.loads(line))

    def test_submit_emits_user_and_stream_events(self):
        agent = FakeAgent()
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)

        task_id = bridge.submit("hello")
        agent.queues[0].put({"next": "he"})
        agent.queues[0].put({"next": "llo"})
        agent.queues[0].put({"done": "hello"})
        bridge.wait_for_idle(timeout=1)

        self.assertEqual(1, task_id)
        self.assertTrue(agent.inc_out)
        self.assertEqual([("hello", "user")], agent.prompts)
        self.assertEqual(
            [
                {"type": "user", "taskId": 1, "text": "hello"},
                {"type": "status", "status": "running", "taskId": 1},
                {"type": "assistant_delta", "taskId": 1, "text": "he"},
                {"type": "assistant_delta", "taskId": 1, "text": "llo"},
                {"type": "assistant_done", "taskId": 1, "text": "hello"},
                {"type": "status", "status": "idle", "taskId": 1},
            ],
            events,
        )

    def test_submit_emits_token_usage_when_backend_usage_changes(self):
        agent = FakeAgent()
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)

        bridge.submit("hello")
        agent.llmclient.backend.last_usage_tokens = {
            "input_tokens": 11,
            "output_tokens": 17,
            "total_tokens": 28,
        }
        agent.queues[0].put({"done": "hello"})
        bridge.wait_for_idle(timeout=1)

        self.assertIn(
            {"type": "token_usage", "taskId": 1, "inputTokens": 11, "outputTokens": 17, "totalTokens": 28},
            events,
        )

    def test_busy_submit_is_rejected_without_calling_agent(self):
        agent = FakeAgent()
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)

        bridge.submit("first")
        result = bridge.submit("second")

        self.assertEqual(-1, result)
        self.assertEqual([("first", "user")], agent.prompts)
        self.assertEqual({"type": "error", "code": "busy", "message": "agent is running"}, events[-1])

    def test_new_session_replaces_agent_and_resets_visible_state(self):
        agents = [FakeAgent(), FakeAgent()]
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agents.pop(0), emit=events.append)
        old_agent = bridge.agent

        bridge.submit("first")
        old_agent.queues[0].put({"done": "first reply"})
        bridge.wait_for_idle(timeout=1)

        event_count = len(events)
        bridge.new_session()
        new_session_events = events[event_count:]
        task_id = bridge.submit("second")
        bridge.agent.queues[0].put({"done": "second reply"})
        bridge.wait_for_idle(timeout=1)

        self.assertIsNot(old_agent, bridge.agent)
        self.assertTrue(old_agent.aborted)
        self.assertEqual(1, task_id)
        self.assertEqual([], old_agent.prompts[1:])
        self.assertEqual([("second", "user")], bridge.agent.prompts)
        self.assertEqual([
            {"type": "history_replace", "messages": []},
            {"type": "system", "text": "Started a new session."},
            {"type": "status", "status": "idle"},
        ], new_session_events)

    def test_new_session_is_rejected_while_agent_is_running(self):
        agent = FakeAgent()
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)

        bridge.submit("busy")
        bridge.new_session()

        self.assertIs(agent, bridge.agent)
        self.assertEqual({"type": "error", "code": "busy", "message": "agent is running"}, events[-1])

    def test_stop_aborts_running_agent(self):
        agent = FakeAgent()
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)

        bridge.submit("first")
        bridge.stop()

        self.assertTrue(agent.aborted)
        self.assertEqual({"type": "status", "status": "stopping"}, events[-1])

    def test_list_resume_sessions_emits_picker_options(self):
        agent = FakeAgent()
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)

        with patch("ink_bridge.continue_list", return_value=[("session-a.txt", 1000.0, "first prompt", 2)]):
            bridge.list_resume_sessions()

        self.assertEqual(
            {
                "type": "resume_sessions",
                "sessions": [
                    {
                        "id": "session-a.txt",
                        "mtime": 1000.0,
                        "preview": "first prompt",
                        "rounds": 2,
                    }
                ],
            },
            events[-1],
        )

    def test_list_resume_sessions_excludes_current_log_and_session(self):
        agent = FakeAgent()
        agent.log_path = "current-log.txt"
        agent.session_id = "session_current"
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)

        with patch("ink_bridge.continue_list", return_value=[]) as list_sessions:
            bridge.list_resume_sessions()

        list_sessions.assert_called_once_with(
            exclude_pid=os.getpid(),
            exclude_path="current-log.txt",
            exclude_session_id="session_current",
        )

    def test_resume_session_by_index_excludes_current_log_and_session(self):
        agent = FakeAgent()
        agent.log_path = "current-log.txt"
        agent.session_id = "session_current"
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)

        with (
            patch("ink_bridge.continue_list", return_value=[("session-a.txt", 1000.0, "first prompt", 2)]) as list_sessions,
            patch.object(bridge, "resume_session") as resume_session,
        ):
            bridge.resume_session_by_index(1)

        list_sessions.assert_called_once_with(
            exclude_pid=os.getpid(),
            exclude_path="current-log.txt",
            exclude_session_id="session_current",
        )
        resume_session.assert_called_once_with("session-a.txt")

    def test_resume_session_replaces_history_messages(self):
        agent = FakeAgent()
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)

        with (
            patch("ink_bridge.continue_reset") as reset,
            patch("ink_bridge.continue_restore", side_effect=lambda agent, _path: (
                setattr(agent.llmclient.backend, "history", [
                    {"role": "user", "content": "old q"},
                    {"role": "assistant", "content": "old a"},
                ]) or ("✅ 已恢复 1 轮完整对话", True)
            )) as restore,
            patch("ink_bridge.continue_extract", return_value=[
                {"role": "user", "content": "old q"},
                {"role": "assistant", "content": "old a"},
            ]),
        ):
            bridge.resume_session("session-a.txt")

        reset.assert_called_once_with(agent, message=None)
        restore.assert_called_once_with(agent, "session-a.txt")
        self.assertIn(
            {
                "type": "history_replace",
                "messages": [
                    {"role": "user", "text": "old q", "taskId": 1},
                    {"role": "assistant", "text": "old a", "taskId": 1},
                ],
            },
            events,
        )
        bridge.rewind(1)
        self.assertEqual([], agent.llmclient.backend.history)
        self.assertEqual({"type": "rewind_done", "taskId": 1, "text": "old q"}, events[-1])

    def test_resume_transcript_uses_exact_backend_history_before_each_turn_for_rewind(self):
        import session_transcript

        with tempfile.TemporaryDirectory() as tmp:
            transcript = session_transcript.create_session(root=tmp, cwd="C:/repo", session_id="session_test")
            first_after = [
                {"role": "system", "content": "seed"},
                {"role": "user", "content": "one"},
                {"role": "assistant", "content": "a1"},
            ]
            second_after = first_after + [
                {"role": "tool", "content": "extra"},
                {"role": "user", "content": "two"},
                {"role": "assistant", "content": "a2"},
            ]
            session_transcript.record_turn(
                transcript,
                session_id="session_test",
                turn_id=1,
                source="user",
                user_text="one",
                assistant_text="a1",
                backend_history_before=[],
                backend_history_after=first_after,
            )
            session_transcript.record_turn(
                transcript,
                session_id="session_test",
                turn_id=2,
                source="user",
                user_text="two",
                assistant_text="a2",
                backend_history_before=first_after,
                backend_history_after=second_after,
            )
            agent = FakeAgent()
            events = []
            bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)

            with patch("ink_bridge.continue_reset", lambda *_args, **_kwargs: None):
                bridge.resume_session(str(transcript))

            self.assertEqual([], bridge._rewind_snapshots[1]["backend_history"])
            self.assertEqual(first_after, bridge._rewind_snapshots[2]["backend_history"])

    def test_resume_transcript_replaces_ui_with_full_conversation_from_start(self):
        import session_transcript

        with tempfile.TemporaryDirectory() as tmp:
            transcript = session_transcript.create_session(root=tmp, cwd="C:/repo", session_id="session_test")
            session_transcript.record_turn(
                transcript,
                session_id="session_test",
                turn_id=1,
                source="user",
                user_text="高斯分布是什么",
                assistant_text="高斯分布回答",
                backend_history_before=[],
                backend_history_after=[{"role": "user", "content": "高斯分布是什么"}],
            )
            session_transcript.record_turn(
                transcript,
                session_id="session_test",
                turn_id=2,
                source="user",
                user_text="高斯生平",
                assistant_text="高斯生平回答",
                backend_history_before=[{"role": "user", "content": "高斯分布是什么"}],
                backend_history_after=[{"role": "user", "content": "高斯生平"}],
            )
            agent = FakeAgent()
            events = []
            bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)

            with patch("ink_bridge.continue_reset", lambda *_args, **_kwargs: None):
                bridge.resume_session(str(transcript))

            history_replace = next(event for event in events if event["type"] == "history_replace")
            self.assertEqual(
                [
                    {"role": "user", "text": "高斯分布是什么", "taskId": 1},
                    {"role": "assistant", "text": "高斯分布回答", "taskId": 1},
                    {"role": "user", "text": "高斯生平", "taskId": 2},
                    {"role": "assistant", "text": "高斯生平回答", "taskId": 2},
                ],
                history_replace["messages"],
            )

    def test_resume_transcript_switches_active_session_and_next_turn_appends_to_same_file(self):
        import session_transcript

        with tempfile.TemporaryDirectory() as tmp:
            transcript = session_transcript.create_session(root=tmp, cwd="C:/repo", session_id="session_old")
            first_after = [{"role": "user", "content": "old question"}]
            session_transcript.record_turn(
                transcript,
                session_id="session_old",
                turn_id=1,
                source="user",
                user_text="old question",
                assistant_text="old answer",
                backend_history_before=[],
                backend_history_after=first_after,
            )
            agent = FakeAgent()
            agent.session_id = "session_new"
            agent.session_path = str(Path(tmp) / "session_new.jsonl")
            agent.session_turn_id = 0
            events = []
            bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)

            with patch("ink_bridge.continue_reset", lambda *_args, **_kwargs: None):
                bridge.resume_session(str(transcript))
            agent.llmclient.backend.history = first_after + [{"role": "user", "content": "follow up"}]
            session_transcript.record_agent_turn(
                agent,
                user_text="follow up",
                assistant_text="follow answer",
                source="user",
                backend_history_before=first_after,
            )

            loaded = session_transcript.load_session(transcript)
            self.assertEqual("session_old", agent.session_id)
            self.assertEqual(str(transcript), agent.session_path)
            self.assertEqual(2, agent.session_turn_id)
            self.assertEqual(["old question", "follow up"], [turn.user_text for turn in loaded.turns])
            self.assertFalse(Path(agent.session_path).with_name("session_new.jsonl").exists())

    def test_rewind_restores_checkpoint_before_selected_task(self):
        agent = FakeAgent()
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)

        agent.history = ["before"]
        agent.llmclient.backend.history = [{"role": "user", "content": "before"}]
        bridge.submit("first")
        agent.queues[0].put({"done": "first done"})
        bridge.wait_for_idle(timeout=1)
        first_history = copy.deepcopy(agent.llmclient.backend.history)
        bridge.submit("second")
        agent.queues[1].put({"done": "second done"})
        bridge.wait_for_idle(timeout=1)

        bridge.rewind(2)

        self.assertEqual(["before", "[USER]: first"], agent.history)
        self.assertEqual(first_history, agent.llmclient.backend.history)
        self.assertIsNone(agent.handler)
        self.assertEqual({"type": "rewind_done", "taskId": 2, "text": "second"}, events[-1])

    def test_rewind_records_transcript_branch_boundary_and_resets_next_turn_id(self):
        import session_transcript

        with tempfile.TemporaryDirectory() as tmp:
            transcript = session_transcript.create_session(root=tmp, cwd="C:/repo", session_id="session_test")
            first_after = [
                {"role": "user", "content": "1+1= ?"},
                {"role": "assistant", "content": "2"},
            ]
            second_after = first_after + [
                {"role": "user", "content": "1-2= ?"},
                {"role": "assistant", "content": "-1"},
            ]
            agent = FakeAgent()
            agent.session_id = "session_test"
            agent.session_path = str(transcript)
            agent.session_turn_id = 2
            agent.llmclient.backend.history = copy.deepcopy(second_after)
            events = []
            bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)
            bridge._task_seq = 2
            bridge._rewind_snapshots = {
                1: {"text": "1+1= ?", "history": [], "backend_history": [], "last_tools": ""},
                2: {"text": "1-2= ?", "history": [], "backend_history": first_after, "last_tools": ""},
            }
            session_transcript.record_turn(
                transcript,
                session_id="session_test",
                turn_id=1,
                source="user",
                user_text="1+1= ?",
                assistant_text="2",
                backend_history_before=[],
                backend_history_after=first_after,
            )
            session_transcript.record_turn(
                transcript,
                session_id="session_test",
                turn_id=2,
                source="user",
                user_text="1-2= ?",
                assistant_text="-1",
                backend_history_before=first_after,
                backend_history_after=second_after,
            )

            bridge.rewind(1)
            agent.llmclient.backend.history = [
                {"role": "user", "content": "1+10= ?"},
                {"role": "assistant", "content": "11"},
            ]
            session_transcript.record_agent_turn(
                agent,
                user_text="1+10= ?",
                assistant_text="11",
                source="user",
                backend_history_before=[],
            )

            loaded = session_transcript.load_session(transcript)
            raw_types = [
                json.loads(line)["type"]
                for line in Path(transcript).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual({"type": "rewind_done", "taskId": 1, "text": "1+1= ?"}, events[-1])
            self.assertEqual(1, agent.session_turn_id)
            self.assertEqual(["session_start", "turn", "turn", "rewind", "turn"], raw_types)
            self.assertEqual(["1+10= ?"], [turn.user_text for turn in loaded.turns])

    def test_emit_mcp_status(self):
        agent = FakeAgent()
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)
        payload = {"config_path": "mcp.json", "servers": [], "tools": [], "errors": {}}

        with patch("mcp_runtime.mcp_status", return_value=payload):
            bridge.mcp_status()

        self.assertEqual({"type": "mcp_status", **payload}, events[-1])

    def test_emit_mcp_action_result_then_status(self):
        agent = FakeAgent()
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)
        payload = {"config_path": "mcp.json", "servers": [], "tools": [], "errors": {}}

        with (
            patch("mcp_runtime.reconnect_mcp_server", return_value={"server": {"name": "demo", "status": "connected"}}),
            patch("mcp_runtime.mcp_status", return_value=payload),
        ):
            bridge.mcp_reconnect("demo")

        self.assertEqual({"type": "system", "text": "MCP server demo reconnect: connected"}, events[-2])
        self.assertEqual({"type": "mcp_status", **payload}, events[-1])

    def test_model_status_emits_available_models(self):
        agent = FakeAgent()
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)

        bridge.model_status()

        self.assertEqual(
            {
                "type": "model_status",
                "models": [
                    {"index": 0, "name": "NativeOAISession/gpt-native", "current": True},
                    {"index": 1, "name": "NativeOAISession/kimi-native", "current": False},
                ],
            },
            events[-1],
        )

    def test_model_switch_uses_agent_selector_then_emits_status(self):
        agent = FakeAgent()
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)

        bridge.model_switch("kimi")

        self.assertEqual(1, agent.llm_no)
        self.assertEqual({"type": "model_switch_result", "ok": True, "message": "Set model to NativeOAISession/kimi-native"}, events[-2])
        self.assertEqual("model_status", events[-1]["type"])
        self.assertTrue(events[-1]["models"][1]["current"])

    def test_model_switch_rejects_while_busy(self):
        agent = FakeAgent()
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)
        bridge.submit("busy")

        bridge.model_switch("kimi")

        self.assertEqual(0, agent.llm_no)
        self.assertEqual({"type": "error", "code": "busy", "message": "agent is running"}, events[-1])

    def test_compact_replaces_backend_history_and_clears_rewind_checkpoints(self):
        agent = FakeAgent()
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)
        agent.llmclient.backend.history = [
            {"role": "user", "content": [{"type": "text", "text": "old context"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "old answer"}]},
        ]
        bridge._rewind_snapshots[1] = {"backend_history": [{"role": "user", "content": "old"}]}

        with patch("ink_bridge.compact_agent_context") as compact:
            compact.return_value.ok = True
            compact.return_value.summary = "summary text"
            compact.return_value.original_messages = 2
            compact.return_value.compacted_messages = 2
            compact.return_value.message = "Compacted 2 messages into summary context."
            bridge.compact("keep file names")

        compact.assert_called_once()
        self.assertEqual({}, bridge._rewind_snapshots)
        self.assertEqual({"type": "status", "status": "running"}, events[-6])
        self.assertEqual({"type": "activity", "label": "Compacting conversation"}, events[-5])
        self.assertEqual(
            {"type": "local_command_output", "text": "Compacted 2 messages into summary context."},
            events[-4],
        )
        self.assertEqual(
            {
                "type": "history_replace",
                "messages": [
                    {"role": "system", "text": "Compacted 2 messages into summary context."},
                ],
            },
            events[-3],
        )
        self.assertEqual({"type": "activity", "label": None}, events[-2])
        self.assertEqual({"type": "status", "status": "idle"}, events[-1])

    def test_compact_records_transcript_compact_event(self):
        import session_transcript

        with tempfile.TemporaryDirectory() as tmp:
            transcript = session_transcript.create_session(root=tmp, cwd="C:/repo", session_id="session_test")
            agent = FakeAgent()
            agent.session_id = "session_test"
            agent.session_path = str(transcript)
            events = []
            bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)
            compacted = [{"role": "user", "content": "summary"}]

            with patch("ink_bridge.compact_agent_context") as compact:
                compact.return_value.ok = True
                compact.return_value.summary = "summary text"
                compact.return_value.original_messages = 4
                compact.return_value.compacted_messages = 1
                compact.return_value.message = "Compacted 4 messages into summary context."
                agent.llmclient.backend.history = compacted
                bridge.compact("keep important details")

            loaded = session_transcript.load_session(transcript)
            self.assertEqual(compacted, loaded.backend_history)

    def test_compact_rejects_while_busy(self):
        agent = FakeAgent()
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)
        bridge.submit("busy")

        bridge.compact("")

        self.assertEqual({"type": "error", "code": "busy", "message": "agent is running"}, events[-1])

    def test_manual_compact_failure_is_reported_as_local_command_output(self):
        agent = FakeAgent()
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)

        with patch("ink_bridge.compact_agent_context") as compact:
            compact.return_value.ok = False
            compact.return_value.message = "No conversation history to compact."
            bridge.compact("")

        self.assertEqual({"type": "status", "status": "running"}, events[-5])
        self.assertEqual({"type": "activity", "label": "Compacting conversation"}, events[-4])
        self.assertEqual(
            {"type": "local_command_output", "text": "Compact failed: No conversation history to compact."},
            events[-3],
        )
        self.assertEqual({"type": "activity", "label": None}, events[-2])
        self.assertEqual({"type": "status", "status": "idle"}, events[-1])

    def test_submit_stops_when_required_auto_compact_fails(self):
        agent = FakeAgent()
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)

        with (
            patch("ink_bridge.should_auto_compact_agent", return_value=True),
            patch("ink_bridge.compact_agent_context") as compact,
        ):
            compact.return_value.ok = False
            compact.return_value.message = "summary failed"
            result = bridge.submit("large prompt")

        self.assertEqual(-1, result)
        self.assertEqual([], agent.prompts)
        self.assertEqual({"type": "error", "code": "auto_compact_failed", "message": "summary failed"}, events[-1])

    def test_submit_auto_compact_replaces_visible_history_before_new_user_message(self):
        agent = FakeAgent()
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)

        with (
            patch("ink_bridge.should_auto_compact_agent", return_value=True),
            patch("ink_bridge.compact_agent_context") as compact,
        ):
            compact.return_value.ok = True
            compact.return_value.message = "Compacted 8 messages into summary context."
            result = bridge.submit("follow up")

        bridge.wait_for_idle(timeout=1)

        self.assertEqual(1, result)
        history_replace_index = next(index for index, event in enumerate(events) if event["type"] == "history_replace")
        user_index = next(index for index, event in enumerate(events) if event["type"] == "user")
        self.assertLess(history_replace_index, user_index)
        self.assertEqual(
            {
                "type": "history_replace",
                "messages": [
                    {"role": "system", "text": "Auto Compacted 8 messages into summary context."},
                ],
            },
            events[history_replace_index],
        )

    def test_skill_status_emits_discovered_skill_metadata(self):
        agent = FakeAgent()
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)

        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp) / "skills" / "demo"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\n"
                "name: demo\n"
                "description: Demo skill\n"
                "---\n"
                "# Demo\n",
                encoding="utf-8",
            )

            bridge.skill_status(search_roots=[str(Path(tmp) / "skills")])

        self.assertEqual("skill_status", events[-1]["type"])
        self.assertEqual(
            [
                {
                    "name": "demo",
                    "description": "Demo skill",
                    "source": "local",
                    "path": str((skill_dir / "SKILL.md").resolve(strict=False)),
                }
            ],
            events[-1]["skills"],
        )

    def test_skill_invoke_loads_skill_name_and_submits_args_as_request(self):
        agent = FakeAgent()
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)

        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp) / "skills" / "demo"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\n"
                "name: demo\n"
                "description: Demo skill\n"
                "---\n"
                "Use $ARGUMENTS from ${GA_SKILL_DIR}.",
                encoding="utf-8",
            )

            bridge.skill_invoke("demo", "中文 args with spaces", search_roots=[str(Path(tmp) / "skills")])

        self.assertEqual(1, len(agent.prompts))
        prompt, source = agent.prompts[0]
        self.assertEqual("user", source)
        self.assertIn('The user invoked skill "demo"', prompt)
        self.assertIn("Use 中文 args with spaces from", prompt)
        self.assertIn("<arguments>\n中文 args with spaces\n</arguments>", prompt)
        self.assertEqual("user", events[0]["type"])
        self.assertEqual("/demo 中文 args with spaces", events[0]["text"])
        self.assertNotIn("<skill>", events[0]["text"])
        self.assertEqual("status", events[1]["type"])


    def test_workflow_draft_creates_run_and_emits_approval_event(self):
        agent = FakeAgent()
        agent.session_id = "session_workflow"
        events = []
        with tempfile.TemporaryDirectory() as tmp:
            bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append, workflow_root=tmp)

            run_id = bridge.workflow_draft('export const meta = { name: "demo" }\nreturn { ok: true }')

            self.assertTrue(run_id.startswith("wf_"))
            self.assertEqual("workflow_draft", events[-2]["type"])
            self.assertEqual(run_id, events[-2]["run"]["runId"])
            self.assertEqual("awaiting_approval", events[-2]["run"]["status"])
            self.assertEqual("workflow_event", events[-1]["type"])
            self.assertEqual("workflow_approval_requested", events[-1]["event"]["type"])
            self.assertEqual(run_id, events[-1]["event"]["runId"])

    def test_workflow_approve_runs_runtime_and_emits_final_result(self):
        agent = FakeAgent()
        agent.session_id = "session_workflow"
        events = []

        class FakeRuntime:
            def __init__(self, *, store, timeout_seconds=10.0):
                self.store = store
                self.timeout_seconds = timeout_seconds

            def run(self, run, *, args=None, resume_from_run_id=None):
                from workflow_models import WorkflowEvent
                self.store.append_event(run, WorkflowEvent(run_id=run.run_id, session_id=run.session_id, event_type="workflow_log", sequence=99, payload={"message": "runtime saw args", "args": args, "resumeFromRunId": resume_from_run_id}))
                payload = {"runId": run.run_id, "status": "succeeded", "result": {"ok": True, "args": args}}
                self.store.write_final_result(run, payload)
                run.status = "succeeded"
                self.store.save_run(run)
                return type("RuntimeResult", (), {"run": run, "result": payload["result"]})()

        with tempfile.TemporaryDirectory() as tmp:
            bridge = GenericAgentBridge(
                agent_factory=lambda: agent,
                emit=events.append,
                workflow_root=tmp,
                workflow_runtime_factory=lambda **kwargs: FakeRuntime(**kwargs),
            )
            run_id = bridge.workflow_draft("return { ok: true }")
            approved = bridge.workflow_approve(run_id, args={"value": 7}, timeout_seconds=3)
            bridge.wait_for_workflow_idle(run_id, timeout=1)

            self.assertTrue(approved)
            event_types = [event["type"] for event in events]
            self.assertIn("workflow_run", event_types)
            self.assertIn("workflow_final", event_types)
            final_event = next(event for event in events if event["type"] == "workflow_final")
            self.assertEqual("succeeded", final_event["result"]["status"])
            self.assertEqual({"ok": True, "args": {"value": 7}}, final_event["result"]["result"])
            workflow_events = [event["event"]["type"] for event in events if event["type"] == "workflow_event"]
            self.assertIn("workflow_started", workflow_events)
            self.assertIn("workflow_log", workflow_events)

    def test_workflow_resume_runs_new_run_with_resume_source(self):
        agent = FakeAgent()
        agent.session_id = "session_workflow"
        events = []
        runtime_calls = []

        class FakeRuntime:
            def __init__(self, *, store, timeout_seconds=10.0):
                self.store = store
                self.timeout_seconds = timeout_seconds

            def run(self, run, *, args=None, resume_from_run_id=None):
                runtime_calls.append({"runId": run.run_id, "args": args, "resumeFromRunId": resume_from_run_id})
                payload = {"runId": run.run_id, "status": "succeeded", "result": {"resumedFrom": resume_from_run_id, "args": args}}
                self.store.write_final_result(run, payload)
                run.status = "succeeded"
                self.store.save_run(run)
                return type("RuntimeResult", (), {"run": run, "result": payload["result"]})()

        with tempfile.TemporaryDirectory() as tmp:
            bridge = GenericAgentBridge(
                agent_factory=lambda: agent,
                emit=events.append,
                workflow_root=tmp,
                workflow_runtime_factory=lambda **kwargs: FakeRuntime(**kwargs),
            )
            source_run_id = bridge.workflow_draft("return { ok: true }")
            source = bridge.workflow_store.load_run(source_run_id)
            source.status = "succeeded"
            bridge.workflow_store.save_run(source)

            resumed = bridge.workflow_resume(source_run_id, args={"value": 9}, timeout_seconds=4)
            bridge.wait_for_workflow_idle(resumed, timeout=1)

            self.assertTrue(resumed.startswith("wf_"))
            self.assertNotEqual(source_run_id, resumed)
            self.assertEqual([{"runId": resumed, "args": {"value": 9}, "resumeFromRunId": source_run_id}], runtime_calls)
            self.assertTrue(any(event["type"] == "workflow_run" and event["run"]["runId"] == resumed for event in events))
            final_event = next(event for event in events if event["type"] == "workflow_final" and event["runId"] == resumed)
            self.assertEqual({"resumedFrom": source_run_id, "args": {"value": 9}}, final_event["result"]["result"])

    def test_workflow_resume_rejects_blank_run_id_with_protocol_error(self):
        agent = FakeAgent()
        events = []
        with tempfile.TemporaryDirectory() as tmp:
            bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append, workflow_root=tmp)

            resumed = bridge.workflow_resume("", args={"value": 1})

        self.assertEqual("", resumed)
        self.assertEqual("error", events[-1]["type"])
        self.assertEqual("workflow_bad_run_id", events[-1]["code"])

    def test_workflow_resume_rejects_unfinished_source_run(self):
        agent = FakeAgent()
        agent.session_id = "session_workflow"
        events = []
        with tempfile.TemporaryDirectory() as tmp:
            bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append, workflow_root=tmp)
            source_run_id = bridge.workflow_draft("return 1")

            resumed = bridge.workflow_resume(source_run_id)

        self.assertEqual("", resumed)
        self.assertEqual("error", events[-1]["type"])
        self.assertEqual("workflow_resume_failed", events[-1]["code"])
        self.assertIn("from awaiting_approval", events[-1]["message"])

    def test_workflow_resume_rejects_denied_cancelled_source_run(self):
        agent = FakeAgent()
        agent.session_id = "session_workflow"
        events = []
        runtime_calls = []

        class FakeRuntime:
            def __init__(self, *, store, timeout_seconds=10.0):
                self.store = store

            def run(self, run, *, args=None, resume_from_run_id=None):
                runtime_calls.append(run.run_id)

        with tempfile.TemporaryDirectory() as tmp:
            bridge = GenericAgentBridge(
                agent_factory=lambda: agent,
                emit=events.append,
                workflow_root=tmp,
                workflow_runtime_factory=lambda **kwargs: FakeRuntime(**kwargs),
            )
            source_run_id = bridge.workflow_draft("return 1")
            bridge.workflow_deny(source_run_id, reason="user deny")

            resumed = bridge.workflow_resume(source_run_id)

        self.assertEqual("", resumed)
        self.assertEqual([], runtime_calls)
        self.assertEqual("error", events[-1]["type"])
        self.assertEqual("workflow_resume_failed", events[-1]["code"])
        self.assertIn("from cancelled", events[-1]["message"])

    def test_workflow_list_detail_and_stop_commands(self):
        agent = FakeAgent()
        agent.session_id = "session_workflow"
        events = []
        with tempfile.TemporaryDirectory() as tmp:
            bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append, workflow_root=tmp)
            run_id = bridge.workflow_draft("return 1")

            bridge.workflow_list()
            bridge.workflow_detail(run_id)
            bridge.workflow_deny(run_id, reason="user deny")

            runs_event = next(event for event in events if event["type"] == "workflow_runs")
            detail_event = next(event for event in events if event["type"] == "workflow_detail")
            run_events = [event for event in events if event["type"] == "workflow_run"]
            self.assertEqual([run_id], [run["runId"] for run in runs_event["runs"]])
            self.assertEqual("return 1", detail_event["script"])
            self.assertEqual("cancelled", run_events[-1]["run"]["status"])
            workflow_events = [event["event"] for event in events if event["type"] == "workflow_event"]
            self.assertIn("workflow_denied", [event["type"] for event in workflow_events])
            denied = next(event for event in workflow_events if event["type"] == "workflow_denied")
            self.assertEqual("user deny", denied["payload"]["reason"])

    def test_workflow_stop_rejects_blank_run_id_with_protocol_error(self):
        agent = FakeAgent()
        events = []
        with tempfile.TemporaryDirectory() as tmp:
            bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append, workflow_root=tmp)

            stopped = bridge.workflow_stop("", reason="user")

        self.assertFalse(stopped)
        self.assertEqual("error", events[-1]["type"])
        self.assertEqual("workflow_bad_run_id", events[-1]["code"])

    def test_stop_stops_active_running_workflow_instead_of_reporting_idle(self):
        agent = FakeAgent()
        agent.session_id = "session_workflow"
        events = []
        runtime_started = threading.Event()
        release_runtime = threading.Event()

        class BlockingRuntime:
            def __init__(self, *, store, timeout_seconds=10.0):
                self.store = store

            def run(self, run, *, args=None, resume_from_run_id=None):
                runtime_started.set()
                release_runtime.wait(timeout=2)
                return None

        with tempfile.TemporaryDirectory() as tmp:
            bridge = GenericAgentBridge(
                agent_factory=lambda: agent,
                emit=events.append,
                workflow_root=tmp,
                workflow_runtime_factory=lambda **kwargs: BlockingRuntime(**kwargs),
            )
            run_id = bridge.workflow_draft("return 1")
            self.assertTrue(bridge.workflow_approve(run_id))
            self.assertTrue(runtime_started.wait(timeout=1))

            bridge.stop()
            release_runtime.set()
            bridge.wait_for_workflow_idle(run_id, timeout=1)

            run = bridge.workflow_store.load_run(run_id)
            self.assertEqual("killed", run.status)
            workflow_events = [event["event"]["type"] for event in events if event["type"] == "workflow_event"]
            self.assertIn("workflow_killed", workflow_events)
            self.assertTrue(any(event["type"] == "workflow_run" and event["run"]["status"] == "killed" for event in events))

    def test_jsonl_loop_routes_mcp_commands(self):
        stdin = io.StringIO(
            json.dumps({"type": "mcp_status"}) + "\n"
            + json.dumps({"type": "mcp_reconnect", "server": "demo"}) + "\n"
            + json.dumps({"type": "mcp_enable", "server": "demo"}) + "\n"
            + json.dumps({"type": "mcp_disable", "server": "demo"}) + "\n"
            + json.dumps({"type": "shutdown"}) + "\n"
        )
        stdout = io.StringIO()

        with patch("ink_bridge.GenericAgentBridge") as bridge_cls:
            bridge = bridge_cls.return_value
            bridge.emit.side_effect = make_stdout_emitter(stdout)
            run_jsonl_loop(stdin, stdout)

        bridge.mcp_status.assert_called_once_with()
        bridge.mcp_reconnect.assert_called_once_with("demo")
        bridge.mcp_enable.assert_called_once_with("demo")
        bridge.mcp_disable.assert_called_once_with("demo")

    def test_jsonl_loop_routes_model_commands(self):
        stdin = io.StringIO(
            json.dumps({"type": "model_status"}) + "\n"
            + json.dumps({"type": "model_switch", "selector": "kimi"}) + "\n"
            + json.dumps({"type": "shutdown"}) + "\n"
        )
        stdout = io.StringIO()

        with patch("ink_bridge.GenericAgentBridge") as bridge_cls:
            bridge = bridge_cls.return_value
            bridge.emit.side_effect = make_stdout_emitter(stdout)
            run_jsonl_loop(stdin, stdout)

        bridge.model_status.assert_called_once_with()
        bridge.model_switch.assert_called_once_with("kimi")

    def test_jsonl_loop_routes_skill_commands(self):
        stdin = io.StringIO(
            json.dumps({"type": "skill_status"}) + "\n"
            + json.dumps({"type": "skill_invoke", "skill": "demo", "args": "hello world"}) + "\n"
            + json.dumps({"type": "shutdown"}) + "\n"
        )
        stdout = io.StringIO()

        with patch("ink_bridge.GenericAgentBridge") as bridge_cls:
            bridge = bridge_cls.return_value
            bridge.emit.side_effect = make_stdout_emitter(stdout)
            run_jsonl_loop(stdin, stdout)

        bridge.skill_status.assert_called_once_with()
        bridge.skill_invoke.assert_called_once_with("demo", "hello world")

    def test_jsonl_loop_routes_compact_command(self):
        stdin = io.StringIO(
            json.dumps({"type": "compact", "instructions": "keep decisions"}) + "\n"
            + json.dumps({"type": "shutdown"}) + "\n"
        )
        stdout = io.StringIO()

        with patch("ink_bridge.GenericAgentBridge") as bridge_cls:
            bridge = bridge_cls.return_value
            bridge.emit.side_effect = make_stdout_emitter(stdout)
            run_jsonl_loop(stdin, stdout)

        bridge.compact.assert_called_once_with("keep decisions")


    def test_jsonl_loop_routes_workflow_commands(self):
        stdin = io.StringIO(
            json.dumps({"type": "workflow_draft", "script": "return 1"}) + "\n"
            + json.dumps({"type": "workflow_approve", "runId": "wf_1", "args": {"x": 1}, "timeoutSeconds": 2}) + "\n"
            + json.dumps({"type": "workflow_resume", "runId": "wf_1", "args": {"y": 2}, "timeoutSeconds": 3}) + "\n"
            + json.dumps({"type": "workflow_list"}) + "\n"
            + json.dumps({"type": "workflow_detail", "runId": "wf_1"}) + "\n"
            + json.dumps({"type": "workflow_deny", "runId": "wf_1", "reason": "no"}) + "\n"
            + json.dumps({"type": "workflow_stop", "runId": "wf_1", "reason": "user"}) + "\n"
            + json.dumps({"type": "shutdown"}) + "\n"
        )
        stdout = io.StringIO()

        with patch("ink_bridge.GenericAgentBridge") as bridge_cls:
            bridge = bridge_cls.return_value
            bridge.emit.side_effect = make_stdout_emitter(stdout)
            run_jsonl_loop(stdin, stdout)

        bridge.workflow_draft.assert_called_once_with("return 1")
        bridge.workflow_approve.assert_called_once_with("wf_1", args={"x": 1}, timeout_seconds=2.0)
        bridge.workflow_resume.assert_called_once_with("wf_1", args={"y": 2}, timeout_seconds=3.0)
        bridge.workflow_list.assert_called_once_with()
        bridge.workflow_detail.assert_called_once_with("wf_1")
        bridge.workflow_deny.assert_called_once_with("wf_1", reason="no")
        bridge.workflow_stop.assert_called_once_with("wf_1", reason="user")

    def test_bridge_script_can_import_agentmain_when_run_from_repo_root(self):
        proc = subprocess.run(
            [sys.executable, str(FRONTENDS / "ink_bridge.py")],
            input=json.dumps({"type": "shutdown"}) + "\n",
            text=True,
            capture_output=True,
            cwd=REPO_ROOT,
            timeout=20,
        )

        self.assertEqual("", proc.stderr)
        self.assertEqual(0, proc.returncode)
        events = [json.loads(line) for line in proc.stdout.splitlines() if line.strip()]
        self.assertEqual("ready", events[0]["type"])


if __name__ == "__main__":
    unittest.main()
