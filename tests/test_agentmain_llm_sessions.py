import queue
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from agentmain import GenericAgent  # noqa: E402


class AgentMainLLMSessionsTest(unittest.TestCase):
    def test_abort_cancels_active_llm_request(self):
        backend = type("Backend", (), {"history": [], "cancelled": False})()
        backend.cancel_current_request = lambda: setattr(backend, "cancelled", True)
        agent = GenericAgent.__new__(GenericAgent)
        agent.is_running = True
        agent.stop_sig = False
        agent.handler = None
        agent.llmclient = type("Client", (), {"backend": backend})()

        agent.abort()

        self.assertTrue(agent.stop_sig)
        self.assertTrue(backend.cancelled)

    def test_abort_stops_mcp_discovery_before_first_model_request(self):
        discovery_started = threading.Event()
        release_discovery = threading.Event()

        class StubClient:
            def __init__(self):
                self.backend = type("Backend", (), {
                    "history": [],
                    "extra_sys_prompt": "",
                    "model": "gpt-test",
                    "cancelled": False,
                })()
                self.last_tools = ""
                self.chat_calls = 0

            def reset_cancel(self):
                self.backend.cancelled = False

            def cancel_current_request(self):
                self.backend.cancelled = True

            def chat(self, messages, tools=None):
                self.chat_calls += 1
                raise AssertionError("cancelled discovery must not reach the model")

        client = StubClient()
        agent = GenericAgent.__new__(GenericAgent)
        agent.lock = threading.Lock()
        agent.task_dir = None
        agent.history = []
        agent.handler = None
        agent.task_queue = queue.Queue()
        agent.is_running = False
        agent.stop_sig = False
        agent.llm_no = 0
        agent.inc_out = False
        agent.verbose = False
        agent.peer_hint = False
        agent.log_path = str(REPO_ROOT / "temp" / "test-agentmain-discovery.log")
        agent.llmclient = client
        agent.llmclients = [client]

        def blocking_load_tool_schema(*_args, **_kwargs):
            from mcp_runtime import _current_stop_signal

            discovery_started.set()
            while not release_discovery.is_set():
                stop_signal = _current_stop_signal()
                if stop_signal is not None and stop_signal.is_set():
                    return
                time.sleep(0.01)

        globals_ref = GenericAgent.run.__globals__
        worker = threading.Thread(target=agent.run, daemon=True)
        with patch.dict(globals_ref, {
            "get_system_prompt": lambda *_args, **_kwargs: "",
            "load_tool_schema": blocking_load_tool_schema,
        }), patch.object(globals_ref["session_transcript"], "current_backend_history", return_value=[]), patch.object(
            globals_ref["session_transcript"], "record_agent_turn", return_value=None
        ):
            worker.start()
            agent.put_task("trigger discovery")
            self.assertTrue(discovery_started.wait(timeout=2), "agent did not enter MCP discovery")
            agent.abort()
            deadline = time.monotonic() + 0.75
            while agent.is_running and time.monotonic() < deadline:
                time.sleep(0.01)
            stopped_quickly = not agent.is_running
            release_discovery.set()
            cleanup_deadline = time.monotonic() + 2
            while agent.is_running and time.monotonic() < cleanup_deadline:
                time.sleep(0.01)

        self.assertTrue(stopped_quickly, "GenericAgent.abort() did not interrupt MCP discovery")
        self.assertEqual(client.chat_calls, 0)

    def test_failed_mixin_config_is_not_left_as_default_client(self):
        # 阶段 3：load_llm_sessions 只走 llm.yaml；失败的 mixin 由
        # load_clients_from_yaml 内部跳过，返回的 clients 里不应混入半残对象。
        ok_client = type("Client", (), {
            "backend": type("Backend", (), {"history": [], "name": "default", "model": "m"})(),
            "last_tools": "",
        })()
        agent = GenericAgent.__new__(GenericAgent)
        agent.llm_no = 0
        import agentmain as agentmain_mod
        agentmain_mod._llm_yaml_path = None
        agentmain_mod._llm_yaml_mtime_ns = None

        def fake_load(**_kwargs):
            return [ok_client], 0, "llm.yaml", 1

        with patch.object(agentmain_mod, "load_clients_from_yaml", side_effect=fake_load):
            agent.load_llm_sessions()

        self.assertEqual(agent.llmclients, [ok_client])
        self.assertIs(agent.llmclient, ok_client)
        self.assertFalse(isinstance(agent.llmclient, dict))


if __name__ == "__main__":
    unittest.main()
