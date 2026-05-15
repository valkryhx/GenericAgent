import json
import subprocess
import queue
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTENDS = REPO_ROOT / "frontends"
if str(FRONTENDS) not in sys.path:
    sys.path.insert(0, str(FRONTENDS))


from ink_bridge import GenericAgentBridge, encode_event  # noqa: E402


class FakeAgent:
    def __init__(self):
        self.inc_out = False
        self.verbose = True
        self.is_running = False
        self.aborted = False
        self.prompts = []
        self.queues = []

    def run(self):
        return None

    def put_task(self, text, source="user"):
        self.prompts.append((text, source))
        dq = queue.Queue()
        self.queues.append(dq)
        return dq

    def abort(self):
        self.aborted = True


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
