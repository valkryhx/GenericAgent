import copy
import io
import json
import subprocess
import queue
import sys
import tempfile
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

    def test_busy_submit_is_rejected_without_calling_agent(self):
        agent = FakeAgent()
        events = []
        bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)

        bridge.submit("first")
        result = bridge.submit("second")

        self.assertEqual(-1, result)
        self.assertEqual([("first", "user")], agent.prompts)
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
                    "path": str(skill_dir / "SKILL.md"),
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
