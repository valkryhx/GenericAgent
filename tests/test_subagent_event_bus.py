import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from agentmain import run_task_worker_loop  # noqa: E402
from subagent_event_bus import SubagentEventBus  # noqa: E402
from subagent_manager import SubagentManager  # noqa: E402
from subagent_state import atomic_write_json  # noqa: E402


class SubagentEventBusTest(unittest.TestCase):
    def test_event_seq_monotonic_and_read_since_cursor(self):
        with tempfile.TemporaryDirectory() as td:
            bus = SubagentEventBus(Path(td) / "temp" / "subagents")

            first = bus.append_event("agent_started", agent_path="/root/a", task_name="a")
            second = bus.append_event("turn_completed", agent_path="/root/a", task_name="a")

            self.assertEqual(first["event_seq"], 1)
            self.assertEqual(second["event_seq"], 2)
            self.assertEqual([event["type"] for event in bus.read_events_since(0)], ["agent_started", "turn_completed"])
            self.assertEqual([event["type"] for event in bus.read_events_since(first["event_seq"])], ["turn_completed"])

    def test_publisher_receives_appended_events_and_failures_do_not_break_durable_append(self):
        with tempfile.TemporaryDirectory() as td:
            published = []

            def publisher(event):
                published.append(event)
                if event["type"] == "boom":
                    raise RuntimeError("realtime channel down")

            bus = SubagentEventBus(Path(td) / "temp" / "subagents", publisher=publisher)

            first = bus.append_event("agent_started", agent_path="/root/a", task_name="a")
            broken = bus.append_event("boom", agent_path="/root/a", task_name="a")

            self.assertEqual([event["type"] for event in published], ["agent_started", "boom"])
            self.assertEqual(published[0]["event_seq"], first["event_seq"])
            self.assertEqual(broken["event_seq"], 2)
            self.assertEqual([event["type"] for event in bus.read_events_since(0)], ["agent_started", "boom"])

    def test_duplicate_event_id_is_not_appended_twice(self):
        with tempfile.TemporaryDirectory() as td:
            bus = SubagentEventBus(Path(td) / "temp" / "subagents")

            first = bus.append_event("agent_started", event_id="evt_fixed", agent_path="/root/a", task_name="a")
            duplicate = bus.append_event("agent_started", event_id="evt_fixed", agent_path="/root/a", task_name="a")

            self.assertEqual(first, duplicate)
            self.assertEqual(len(bus.read_events_since(0)), 1)
            self.assertEqual(bus.next_event_seq(), 2)

    def test_notification_queue_is_consumed_once(self):
        with tempfile.TemporaryDirectory() as td:
            bus = SubagentEventBus(Path(td) / "temp" / "subagents")
            bus.append_event("turn_completed", agent_path="/root/a", task_name="a", notify=True, payload={"summary": "done"})

            first = bus.consume_notifications()
            second = bus.consume_notifications()

            self.assertEqual(len(first), 1)
            self.assertEqual(first[0]["type"], "turn_completed")
            self.assertEqual(first[0]["payload"], {"summary": "done"})
            self.assertEqual(second, [])

    def test_wait_agent_returns_event_bus_events_and_next_cursor(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td) / "temp" / "demo"
            task_dir.mkdir(parents=True)
            atomic_write_json(
                task_dir / "state.json",
                {
                    "schema_version": 1,
                    "task_name": "demo",
                    "agent_path": "/root/demo",
                    "pid": 1,
                    "round": 0,
                    "turn_status": "running",
                    "process_status": "alive",
                },
            )
            manager = SubagentManager(root_dir=td, process_exists=lambda pid: True)
            event = manager.event_bus.append_event(
                "turn_completed",
                agent_path="/root/demo",
                task_name="demo",
                status={"turn_status": "completed", "process_status": "waiting_reply"},
            )

            result = manager.wait_agents(["demo"], timeout_s=0, since_event_seq=0)
            timeout = manager.wait_agents(["demo"], timeout_s=0, since_event_seq=result.next_event_seq)

            self.assertFalse(result.timed_out)
            self.assertEqual(result.events[0]["event_seq"], event["event_seq"])
            self.assertEqual(result.next_event_seq, event["event_seq"])
            self.assertTrue(timeout.timed_out)
            self.assertEqual(timeout.next_event_seq, event["event_seq"])


    def test_concurrent_append_event_assigns_unique_monotonic_event_seq(self):
        with tempfile.TemporaryDirectory() as td:
            bus = SubagentEventBus(Path(td) / "temp" / "subagents")
            start = threading.Barrier(24)
            results = []
            errors = []

            def worker(index):
                try:
                    start.wait(timeout=5)
                    event = bus.append_event("turn_completed", agent_path=f"/root/a_{index}", task_name=f"a_{index}")
                    results.append(event["event_seq"])
                except Exception as exc:
                    errors.append(exc)

            threads = [threading.Thread(target=worker, args=(index,)) for index in range(24)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=5)

            self.assertEqual(errors, [])
            self.assertEqual(len(results), 24)
            self.assertEqual(sorted(results), list(range(1, 25)))
            events = bus.read_events_since(0)
            self.assertEqual(sorted(event["event_seq"] for event in events), list(range(1, 25)))


    def test_worker_turn_completion_writes_global_event_bus(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td) / "temp" / "demo"
            task_dir.mkdir(parents=True)
            (task_dir / "input.txt").write_text("run", encoding="utf-8")

            class DoneQueue:
                def get(self, timeout=None):
                    return {"done": "finished"}

            class FakeAgent:
                peer_hint = True
                task_dir = None
                subagent_permission_policy = None

                def put_task(self, raw, source="task"):
                    return DoneQueue()

            run_task_worker_loop(FakeAgent(), task_dir, reply_wait_iterations=0, reply_sleep_s=0, sleep_fn=lambda _: None)

            events = SubagentEventBus(Path(td) / "temp" / "subagents").read_events_since(0)
            self.assertIn("turn_completed", [event["type"] for event in events])
            completed = [event for event in events if event["type"] == "turn_completed"][-1]
            self.assertEqual(completed["agent_path"], "/root/demo")
            self.assertEqual(completed["status"], {"turn_status": "completed", "process_status": "waiting_reply"})


if __name__ == "__main__":
    unittest.main()
