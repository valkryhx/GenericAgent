import json
import queue
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from agentmain import run_task_worker_loop, start_task_background  # noqa: E402


class FakeAgent:
    def __init__(self, done_text):
        backend = type("Backend", (), {"history": []})()
        self.llmclient = type("Client", (), {"backend": backend})()
        self.done_text = done_text
        self.peer_hint = True
        self.task_dir = None

    def put_task(self, raw, source="task"):
        self.seen_raw = raw
        self.seen_source = source
        result = queue.Queue()
        result.put({"done": self.done_text})
        return result


class MultiRoundFakeAgent:
    def __init__(self, done_texts):
        backend = type("Backend", (), {"history": []})()
        self.llmclient = type("Client", (), {"backend": backend})()
        self.done_texts = list(done_texts)
        self.seen_raws = []
        self.peer_hint = True
        self.task_dir = None

    def put_task(self, raw, source="task"):
        self.seen_raws.append(raw)
        result = queue.Queue()
        result.put({"done": self.done_texts.pop(0)})
        return result


class AgentMainSubagentLifecycleTest(unittest.TestCase):
    def test_start_task_background_writes_initial_state_registry_and_event(self):
        with tempfile.TemporaryDirectory() as td:
            calls = []

            class FakeProcess:
                pid = 13579

            def fake_popen(cmd, **kwargs):
                calls.append((cmd, kwargs))
                return FakeProcess()

            pid = start_task_background(
                "cli_task",
                input_text="long prompt must stay out of child command",
                llm_no=3,
                verbose=True,
                root_dir=td,
                popen=fake_popen,
                python_executable="python-test",
            )

            task_dir = Path(td) / "temp" / "cli_task"
            self.assertEqual(pid, 13579)
            self.assertEqual(
                (task_dir / "input.txt").read_text(encoding="utf-8"),
                "long prompt must stay out of child command",
            )
            cmd, kwargs = calls[0]
            self.assertEqual(cmd[0], "python-test")
            self.assertIn("--task", cmd)
            self.assertIn("cli_task", cmd)
            self.assertIn("--nobg", cmd)
            self.assertIn("--llm_no", cmd)
            self.assertIn("3", cmd)
            self.assertIn("--verbose", cmd)
            self.assertNotIn("long prompt must stay out of child command", cmd)
            self.assertEqual(Path(kwargs["cwd"]), Path(td))
            state = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["pid"], 13579)
            self.assertEqual(state["turn_status"], "pending")
            self.assertEqual(state["process_status"], "alive")
            self.assertEqual(state["input_path"], str(task_dir / "input.txt"))
            events = [
                json.loads(line)
                for line in (task_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(events[-1]["type"], "agent_started")
            self.assertEqual(events[-1]["pid"], 13579)
            registry = json.loads((Path(td) / "temp" / "subagents" / "registry.json").read_text(encoding="utf-8"))
            self.assertEqual(registry["agents"]["/root/cli_task"]["pid"], 13579)

    def test_start_task_background_writes_context_fork_history_when_requested(self):
        with tempfile.TemporaryDirectory() as td:
            history = [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "second"},
                {"role": "user", "content": "third"},
            ]

            class FakeProcess:
                pid = 24680

            def fake_popen(_cmd, **_kwargs):
                return FakeProcess()

            start_task_background(
                "forked_cli_task",
                input_text="new task prompt",
                root_dir=td,
                popen=fake_popen,
                python_executable="python-test",
                fork_turns="2",
                fork_history=history,
            )

            task_dir = Path(td) / "temp" / "forked_cli_task"
            self.assertEqual(
                json.loads((task_dir / "_history.json").read_text(encoding="utf-8")),
                history[-2:],
            )
            state = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["fork_turns"], "2")

    def test_task_worker_loop_writes_output_state_and_events(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td) / "temp" / "demo_task"
            agent = FakeAgent("final answer")

            run_task_worker_loop(
                agent,
                task_dir,
                input_text="do the task",
                reply_wait_iterations=0,
                sleep_fn=lambda _: None,
            )

            self.assertEqual(agent.seen_raw, "do the task")
            self.assertEqual(agent.seen_source, "task")
            self.assertFalse(agent.peer_hint)
            self.assertEqual(agent.task_dir, str(task_dir))
            self.assertEqual(
                (task_dir / "output.txt").read_text(encoding="utf-8"),
                "final answer\n\n[ROUND END]\n",
            )
            state = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["turn_status"], "completed")
            self.assertEqual(state["process_status"], "exited")
            self.assertEqual(state["final_output_path"], str(task_dir / "output.txt"))
            events = [
                json.loads(line)["type"]
                for line in (task_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(
                events,
                ["turn_started", "turn_completed", "agent_waiting_reply", "agent_exited"],
            )
            parent_events = [
                json.loads(line)["type"]
                for line in (Path(td) / "temp" / "subagents" / "inbox.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(parent_events, ["turn_completed", "agent_waiting_reply", "agent_exited"])

    def test_task_worker_loop_stops_gracefully_when_stop_file_appears_during_reply_wait(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td) / "temp" / "demo_stop"
            agent = FakeAgent("final answer")
            sleep_calls = {"count": 0}

            def sleep_fn(_):
                sleep_calls["count"] += 1
                if sleep_calls["count"] == 1:
                    (task_dir / "_stop").parent.mkdir(parents=True, exist_ok=True)
                    (task_dir / "_stop").write_text("stop now", encoding="utf-8")

            run_task_worker_loop(
                agent,
                task_dir,
                input_text="do the task",
                reply_wait_iterations=5,
                reply_sleep_s=0,
                sleep_fn=sleep_fn,
            )

            state = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["turn_status"], "completed")
            self.assertEqual(state["process_status"], "shutdown")
            events = [
                json.loads(line)["type"]
                for line in (task_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertIn("agent_shutdown", events)
            self.assertNotIn("agent_exited", events)

    def test_task_worker_loop_consumes_trigger_message_from_mailbox_without_reply_file(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td) / "temp" / "demo_mailbox"
            agent = MultiRoundFakeAgent(["first answer", "second answer"])
            sleep_calls = {"count": 0}

            def sleep_fn(_):
                sleep_calls["count"] += 1
                if sleep_calls["count"] == 1:
                    row = {
                        "schema_version": 1,
                        "id": "msg_1",
                        "author": "/root",
                        "recipient": "/root/demo_mailbox",
                        "content": "second task from mailbox",
                        "trigger_turn": True,
                        "priority": "normal",
                        "created_at": "2026-07-13T00:00:00+08:00",
                        "consumed_at": None,
                    }
                    (task_dir / "mailbox.jsonl").write_text(
                        json.dumps(row, ensure_ascii=False) + "\n",
                        encoding="utf-8",
                    )

            run_task_worker_loop(
                agent,
                task_dir,
                input_text="first task",
                reply_wait_iterations=1,
                reply_sleep_s=0,
                sleep_fn=sleep_fn,
            )

            self.assertEqual(agent.seen_raws, ["first task", "second task from mailbox"])
            self.assertFalse((task_dir / "reply.txt").exists())
            self.assertEqual(
                (task_dir / "output1.txt").read_text(encoding="utf-8"),
                "second answer\n\n[ROUND END]\n",
            )
            mailbox_rows = [
                json.loads(line)
                for line in (task_dir / "mailbox.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertIsNotNone(mailbox_rows[0]["consumed_at"])
            events = [
                json.loads(line)["type"]
                for line in (task_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(events.count("message_consumed"), 1)

    def test_task_worker_loop_clears_matching_reply_file_after_consuming_mailbox_trigger(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td) / "temp" / "demo_mailbox_reply"
            agent = MultiRoundFakeAgent(["first answer", "second answer"])
            sleep_calls = {"count": 0}

            def sleep_fn(_):
                sleep_calls["count"] += 1
                if sleep_calls["count"] == 1:
                    row = {
                        "schema_version": 1,
                        "id": "msg_1",
                        "author": "/root",
                        "recipient": "/root/demo_mailbox_reply",
                        "content": "second task from mailbox",
                        "trigger_turn": True,
                        "priority": "normal",
                        "created_at": "2026-07-13T00:00:00+08:00",
                        "consumed_at": None,
                    }
                    (task_dir / "mailbox.jsonl").write_text(
                        json.dumps(row, ensure_ascii=False) + "\n",
                        encoding="utf-8",
                    )
                    (task_dir / "reply.txt").write_text("second task from mailbox", encoding="utf-8")

            run_task_worker_loop(
                agent,
                task_dir,
                input_text="first task",
                reply_wait_iterations=1,
                reply_sleep_s=0,
                sleep_fn=sleep_fn,
            )

            self.assertEqual(agent.seen_raws, ["first task", "second task from mailbox"])
            self.assertFalse((task_dir / "reply.txt").exists())


if __name__ == "__main__":
    unittest.main()
