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


from genericagent.core.app import GenericAgent  # noqa: E402


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
        discovery_noticed_abort = threading.Event()
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
            from genericagent.mcp.runtime import _current_stop_signal

            discovery_started.set()
            while not release_discovery.is_set():
                stop_signal = _current_stop_signal()
                if stop_signal is not None and stop_signal.is_set():
                    discovery_noticed_abort.set()
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
            # Abort lands on the discovery loop through the stop signal it polls. That
            # is the behaviour under test, and it is what gets asserted.
            #
            # `is_running == False` is NOT that behaviour: it is only cleared in the
            # `finally` at app.py:2328, after the whole turn unwinds (capability
            # profile, agent_runner_loop construction, stop-file watcher, stream
            # drain). Measured with tests/probes/python/_probe_abort_discovery_latency.py:
            # discovery sees the abort in ~11 ms, but is_running clears at 11 ms only
            # because the stubbed chat raises immediately. Injecting 900 ms into the
            # teardown pushed is_running to 915 ms while discovery still stopped in
            # 11 ms -- so the old 0.75 s window could go red with the behaviour
            # completely intact. The turn teardown gets its own, larger budget below.
            self.assertTrue(
                discovery_noticed_abort.wait(timeout=2),
                "abort() did not reach the blocking MCP discovery loop",
            )
            release_discovery.set()
            teardown_deadline = time.monotonic() + 15.0
            while agent.is_running and time.monotonic() < teardown_deadline:
                time.sleep(0.01)

        self.assertFalse(agent.is_running, "the aborted turn did not finish unwinding")
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
        import genericagent.core.app as agentmain_mod
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
