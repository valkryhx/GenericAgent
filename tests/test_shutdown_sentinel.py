import queue
import threading
import unittest

from genericagent.core.app import GenericAgent


class ShutdownSentinelTest(unittest.TestCase):
    def _bare_agent(self):
        agent = object.__new__(GenericAgent)
        agent.task_queue = queue.Queue()
        agent.is_running = False
        agent.stop_sig = False
        agent._shutdown_requested = False
        return agent

    def test_run_exits_on_shutdown_item(self):
        agent = self._bare_agent()
        agent.task_queue.put({
            "__shutdown__": True,
            "query": "",
            "source": "shutdown",
            "output": queue.Queue(),
        })
        thread = threading.Thread(target=agent.run, daemon=True)
        thread.start()
        thread.join(timeout=2.0)
        self.assertFalse(thread.is_alive())

    def test_request_shutdown_enqueues_sentinel(self):
        agent = self._bare_agent()
        agent.request_shutdown()
        item = agent.task_queue.get_nowait()
        self.assertTrue(item.get("__shutdown__"))
        self.assertTrue(agent._shutdown_requested)


if __name__ == "__main__":
    unittest.main()
