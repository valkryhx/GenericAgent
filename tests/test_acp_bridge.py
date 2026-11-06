import io
import json
import os
import queue
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("GA_ACP_NO_FD_REDIRECT", "1")

from frontends.acp import genericagent_acp_bridge as acp


class FakeRuntime:
    def __init__(self):
        self.emit = None
        self.resolved = []

    def set_emit(self, emit):
        self.emit = emit

    def resolve(self, request_id, decision):
        self.resolved.append((request_id, decision))
        return True

    def cancel_all(self):
        return 0


class FakeAgent:
    last_kwargs = None

    def __init__(self, workspace_root=None):
        self.workspace_root = workspace_root
        self.permission_mode = "full_access"
        self.permission_runtime = FakeRuntime()
        self.tool_event_callback = None
        self.shutdown_called = False
        FakeAgent.last_kwargs = {"workspace_root": workspace_root}

    def next_llm(self, n):
        self.llm_no = n

    def run(self):
        return None

    def put_task(self, text, source="user", images=None):
        q = queue.Queue()
        q.put({"done": "ok"})
        return q

    def abort(self):
        return None

    def request_shutdown(self):
        self.shutdown_called = True

    def set_permission_mode(self, mode):
        self.permission_mode = mode
        return mode


class AcpBridgeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        patcher = patch.object(acp, "GeneraticAgent", FakeAgent)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.bridge = acp.GenericAgentAcpBridge()
        self.bridge._json_out = io.BytesIO()
        self.bridge.handle_session_new(1, {"cwd": self.tmp.name})
        self.session_id = next(iter(self.bridge._sessions))
        self.session = self.bridge._sessions[self.session_id]

    def messages(self):
        raw = self.bridge._json_out.getvalue().decode("utf-8")
        return [json.loads(line) for line in raw.splitlines() if line.strip()]

    def test_workspace_root_is_applied(self):
        self.assertEqual(FakeAgent.last_kwargs["workspace_root"], self.tmp.name)

    def test_permission_request_and_response(self):
        self.session.agent.permission_runtime.emit({
            "type": "permission_request",
            "requestId": "ga-perm-1",
            "toolName": "file_patch",
            "argsPreview": '{"path":"a.txt"}',
        })
        requests = [m for m in self.messages()
                    if m.get("method") == "session/request_permission"]
        self.assertEqual(1, len(requests))
        request = requests[0]
        self.assertEqual(self.session_id, request["params"]["sessionId"])
        self.assertEqual("allow_once", request["params"]["options"][0]["optionId"])

        self.bridge.handle_message({
            "jsonrpc": "2.0",
            "id": request["id"],
            "result": {"outcome": {"outcome": "selected", "optionId": "allow_once"}},
        })
        self.assertEqual([("ga-perm-1", "accept")],
                         self.session.agent.permission_runtime.resolved)

    def test_tool_events_become_tool_calls(self):
        self.bridge._json_out = io.BytesIO()
        self.bridge._on_tool_event(self.session, {
            "phase": "start", "toolName": "file_read",
            "toolUseId": "call-1", "input": {"path": "a.txt"},
        })
        self.bridge._on_tool_event(self.session, {
            "phase": "end", "toolName": "file_read",
            "toolUseId": "call-1", "status": "success", "content": "ok",
        })
        updates = [m["params"]["update"] for m in self.messages()
                   if m.get("method") == "session/update"]
        self.assertEqual("tool_call", updates[0]["sessionUpdate"])
        self.assertEqual("read", updates[0]["kind"])
        self.assertEqual("tool_call_update", updates[1]["sessionUpdate"])
        self.assertEqual("completed", updates[1]["status"])

    def test_set_mode_updates_permission_mode(self):
        self.bridge.handle_message({
            "jsonrpc": "2.0", "id": 7, "method": "session/set_mode",
            "params": {"sessionId": self.session_id, "modeId": "ask"},
        })
        self.assertEqual("ask", self.session.agent.permission_mode)

    def test_close_stops_agent(self):
        self.bridge.handle_message({
            "jsonrpc": "2.0", "id": 8, "method": "session/close",
            "params": {"sessionId": self.session_id},
        })
        self.assertNotIn(self.session_id, self.bridge._sessions)
        self.assertTrue(self.session.agent.shutdown_called)


if __name__ == "__main__":
    unittest.main()
