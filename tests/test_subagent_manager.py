import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from subagent_state import append_jsonl_event, atomic_write_json  # noqa: E402
from subagent_manager import SubagentManager  # noqa: E402


class SubagentManagerReadTest(unittest.TestCase):
    def _write_running_state(self, root, task_name="wc_france_history", pid=1234):
        task_dir = Path(root) / "temp" / task_name
        task_dir.mkdir(parents=True)
        output_path = task_dir / "output.txt"
        output_path.write_text("France final answer\n\n[ROUND END]\n", encoding="utf-8")
        atomic_write_json(
            task_dir / "state.json",
            {
                "schema_version": 1,
                "task_name": task_name,
                "agent_path": f"/root/{task_name}",
                "pid": pid,
                "round": 0,
                "turn_status": "running",
                "process_status": "alive",
                "output_path": str(output_path),
                "final_output_path": None,
            },
        )
        return task_dir

    def test_read_agent_treats_round_end_as_completed_even_when_process_is_alive(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = self._write_running_state(td)
            manager = SubagentManager(root_dir=td, process_exists=lambda pid: pid == 1234)

            state = manager.read_agent("wc_france_history")

            self.assertEqual(state.turn_status, "completed")
            self.assertEqual(state.process_status, "waiting_reply")
            self.assertEqual(Path(state.final_output_path), task_dir / "output.txt")
            persisted = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(persisted["turn_status"], "completed")
            self.assertEqual(persisted["process_status"], "waiting_reply")

    def test_read_agent_does_not_treat_partial_output_as_final(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td) / "temp" / "wc_brazil_history"
            task_dir.mkdir(parents=True)
            output_path = task_dir / "output.txt"
            output_path.write_text("Brazil partial answer without final marker", encoding="utf-8")
            atomic_write_json(
                task_dir / "state.json",
                {
                    "schema_version": 1,
                    "task_name": "wc_brazil_history",
                    "agent_path": "/root/wc_brazil_history",
                    "pid": 4321,
                    "round": 0,
                    "turn_status": "running",
                    "process_status": "alive",
                    "output_path": str(output_path),
                    "final_output_path": None,
                },
            )
            manager = SubagentManager(root_dir=td, process_exists=lambda pid: pid == 4321)

            state = manager.read_agent("wc_brazil_history")

            self.assertEqual(state.turn_status, "running")
            self.assertEqual(state.process_status, "alive")
            self.assertIsNone(state.final_output_path)

    def test_read_agent_does_not_use_previous_round_output_to_complete_current_round(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td) / "temp" / "multi_round"
            task_dir.mkdir(parents=True)
            previous_output = task_dir / "output.txt"
            previous_output.write_text("first round\n\n[ROUND END]\n", encoding="utf-8")
            next_output = task_dir / "output1.txt"
            atomic_write_json(
                task_dir / "state.json",
                {
                    "schema_version": 1,
                    "task_name": "multi_round",
                    "agent_path": "/root/multi_round",
                    "pid": 2222,
                    "round": 1,
                    "turn_status": "running",
                    "process_status": "alive",
                    "output_path": str(next_output),
                    "final_output_path": str(previous_output),
                },
            )
            manager = SubagentManager(root_dir=td, process_exists=lambda pid: pid == 2222)

            state = manager.read_agent("multi_round")

            self.assertEqual(state.round, 1)
            self.assertEqual(state.turn_status, "running")
            self.assertEqual(state.process_status, "alive")
            self.assertEqual(Path(state.output_path), next_output)
            self.assertEqual(Path(state.final_output_path), previous_output)

    def test_close_agent_returns_previous_state_and_preserves_final_output(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = self._write_running_state(td)
            terminated = []
            manager = SubagentManager(
                root_dir=td,
                process_exists=lambda pid: pid == 1234,
                terminate_process=lambda pid: terminated.append(pid),
                sleep=lambda _: None,
            )

            result = manager.close_agent("wc_france_history", reason="parent_cleanup", grace_s=0)

            self.assertEqual(result.previous_state.turn_status, "completed")
            self.assertEqual(result.previous_state.process_status, "waiting_reply")
            self.assertEqual(result.closed_state.process_status, "killed")
            self.assertEqual(Path(result.final_output_path), task_dir / "output.txt")
            self.assertEqual(terminated, [1234])
            self.assertTrue((task_dir / "_stop").exists())
            events = (task_dir / "events.jsonl").read_text(encoding="utf-8")
            self.assertIn('"type":"agent_closed"', events)

    def test_close_agent_records_shutdown_when_process_exits_during_grace_period(self):
        with tempfile.TemporaryDirectory() as td:
            self._write_running_state(td)
            checks = {"count": 0}
            terminated = []

            def process_exists(_pid):
                checks["count"] += 1
                return checks["count"] == 1

            manager = SubagentManager(
                root_dir=td,
                process_exists=process_exists,
                terminate_process=lambda pid: terminated.append(pid),
                sleep=lambda _: None,
            )

            result = manager.close_agent("wc_france_history", reason="parent_cleanup", grace_s=1)

            self.assertEqual(result.previous_state.turn_status, "completed")
            self.assertEqual(result.closed_state.process_status, "shutdown")
            self.assertEqual(terminated, [])

    def test_read_agent_preserves_closed_process_status_after_shutdown(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = self._write_running_state(td)
            state_path = task_dir / "state.json"
            raw = json.loads(state_path.read_text(encoding="utf-8"))
            raw["turn_status"] = "completed"
            raw["process_status"] = "shutdown"
            raw["final_output_path"] = raw["output_path"]
            atomic_write_json(state_path, raw)
            manager = SubagentManager(root_dir=td, process_exists=lambda pid: False)

            state = manager.read_agent("wc_france_history")

            self.assertEqual(state.turn_status, "completed")
            self.assertEqual(state.process_status, "shutdown")


class SubagentManagerSpawnWaitMailboxTest(unittest.TestCase):
    def test_spawn_agent_writes_input_state_and_registry_without_putting_prompt_on_command_line(self):
        with tempfile.TemporaryDirectory() as td:
            calls = []

            class FakeProcess:
                pid = 2468

            def fake_popen(cmd, **kwargs):
                calls.append((cmd, kwargs))
                return FakeProcess()

            manager = SubagentManager(root_dir=td, popen=fake_popen, python_executable="python-test")

            handle = manager.spawn_agent(
                "research_france",
                "long task prompt",
                llm_no=2,
                verbose=True,
                parent_session_id="session_demo",
            )

            task_dir = Path(td) / "temp" / "research_france"
            self.assertEqual(handle.task_name, "research_france")
            self.assertEqual(handle.pid, 2468)
            self.assertEqual((task_dir / "input.txt").read_text(encoding="utf-8"), "long task prompt")
            cmd, kwargs = calls[0]
            self.assertEqual(cmd[0], "python-test")
            self.assertIn("--task", cmd)
            self.assertIn("research_france", cmd)
            self.assertIn("--nobg", cmd)
            self.assertIn("--llm_no", cmd)
            self.assertIn("2", cmd)
            self.assertIn("--verbose", cmd)
            self.assertNotIn("long task prompt", cmd)
            self.assertEqual(Path(kwargs["cwd"]), Path(td))
            state = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["turn_status"], "pending")
            self.assertEqual(state["process_status"], "alive")
            registry = json.loads((Path(td) / "temp" / "subagents" / "registry.json").read_text(encoding="utf-8"))
            self.assertEqual(registry["agents"]["/root/research_france"]["pid"], 2468)
            self.assertEqual(
                registry["agents"]["/root/research_france"]["last_task_message"],
                "long task prompt",
            )

    def test_spawn_agent_rejects_unsafe_task_names(self):
        with tempfile.TemporaryDirectory() as td:
            manager = SubagentManager(root_dir=td, popen=lambda *_, **__: None)

            for bad_name in ["../escape", "UpperCase", "has-dash", "space name", ""]:
                with self.subTest(bad_name=bad_name):
                    with self.assertRaises(ValueError):
                        manager.spawn_agent(bad_name, "prompt")

    def test_spawn_agent_fork_turns_all_writes_full_history(self):
        with tempfile.TemporaryDirectory() as td:
            manager = SubagentManager(
                root_dir=td,
                popen=lambda *_, **__: type("FakeProcess", (), {"pid": 1})(),
                python_executable="python-test",
            )
            history = [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "second"},
            ]

            manager.spawn_agent("fork_all", "prompt", fork_turns="all", fork_history=history)

            task_dir = Path(td) / "temp" / "fork_all"
            self.assertEqual(
                json.loads((task_dir / "_history.json").read_text(encoding="utf-8")),
                history,
            )
            state = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["fork_turns"], "all")

    def test_spawn_agent_fork_turns_last_n_writes_recent_history(self):
        with tempfile.TemporaryDirectory() as td:
            manager = SubagentManager(
                root_dir=td,
                popen=lambda *_, **__: type("FakeProcess", (), {"pid": 1})(),
                python_executable="python-test",
            )
            history = [
                {"role": "user", "content": "one"},
                {"role": "assistant", "content": "two"},
                {"role": "user", "content": "three"},
            ]

            manager.spawn_agent("fork_recent", "prompt", fork_turns="2", fork_history=history)

            task_dir = Path(td) / "temp" / "fork_recent"
            self.assertEqual(
                json.loads((task_dir / "_history.json").read_text(encoding="utf-8")),
                history[-2:],
            )
            state = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["fork_turns"], "2")

    def test_spawn_agent_fork_turns_none_does_not_write_history(self):
        with tempfile.TemporaryDirectory() as td:
            manager = SubagentManager(
                root_dir=td,
                popen=lambda *_, **__: type("FakeProcess", (), {"pid": 1})(),
                python_executable="python-test",
            )

            manager.spawn_agent(
                "fork_none",
                "prompt",
                fork_turns="none",
                fork_history=[{"role": "user", "content": "ignored"}],
            )

            self.assertFalse((Path(td) / "temp" / "fork_none" / "_history.json").exists())

    def test_spawn_agent_rejects_invalid_fork_turns(self):
        with tempfile.TemporaryDirectory() as td:
            manager = SubagentManager(root_dir=td, popen=lambda *_, **__: None)

            for bad_value in ["", "0", "-1", "abc"]:
                with self.subTest(bad_value=bad_value):
                    with self.assertRaises(ValueError):
                        manager.spawn_agent("fork_bad", "prompt", fork_turns=bad_value, fork_history=[])

    def test_spawn_agent_rejects_missing_history_when_fork_requested(self):
        with tempfile.TemporaryDirectory() as td:
            manager = SubagentManager(root_dir=td, popen=lambda *_, **__: None)

            with self.assertRaises(ValueError):
                manager.spawn_agent("fork_missing", "prompt", fork_turns="all")

    def test_wait_agents_returns_when_events_file_changes(self):
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

            def append_later():
                time.sleep(0.05)
                (task_dir / "events.jsonl").write_text('{"type":"turn_completed"}\n', encoding="utf-8")

            thread = threading.Thread(target=append_later)
            thread.start()
            result = manager.wait_agents(["demo"], timeout_s=1, poll_interval_s=0.01)
            thread.join(timeout=1)

            self.assertFalse(result.timed_out)
            self.assertEqual([state.task_name for state in result.changed_agents], ["demo"])

    def test_wait_agents_returns_when_parent_inbox_receives_update(self):
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

            def notify_later():
                time.sleep(0.05)
                append_jsonl_event(
                    Path(td) / "temp" / "subagents" / "inbox.jsonl",
                    {"type": "turn_completed", "task_name": "demo", "agent_path": "/root/demo"},
                )

            thread = threading.Thread(target=notify_later)
            thread.start()
            result = manager.wait_agents(["demo"], timeout_s=1, poll_interval_s=0.01)
            thread.join(timeout=1)

            self.assertFalse(result.timed_out)
            self.assertEqual([state.task_name for state in result.changed_agents], ["demo"])
            self.assertEqual(result.message, "Subagent mailbox update received.")
            self.assertEqual(result.events[0]["type"], "turn_completed")

    def test_list_agents_ignores_non_agent_temp_dirs(self):
        with tempfile.TemporaryDirectory() as td:
            temp_root = Path(td) / "temp"
            temp_root.mkdir(parents=True)
            for name in [".playwright-cli", "ga-real-e2e-tmp", "model_responses", "sessions"]:
                (temp_root / name).mkdir()
            (temp_root / "ga-real-e2e-tmp" / "state.json").write_text("{}", encoding="utf-8")
            state_less_dir = temp_root / "valid_no_state"
            state_less_dir.mkdir()
            task_dir = temp_root / "demo_agent"
            task_dir.mkdir()
            atomic_write_json(
                task_dir / "state.json",
                {
                    "schema_version": 1,
                    "task_name": "demo_agent",
                    "agent_path": "/root/demo_agent",
                    "pid": 1,
                    "round": 0,
                    "turn_status": "running",
                    "process_status": "alive",
                },
            )
            manager = SubagentManager(root_dir=td, process_exists=lambda pid: True)

            states = manager.list_agents()
            result = manager.wait_agents([], timeout_s=1, poll_interval_s=0.01)

            self.assertEqual([state.task_name for state in states], ["demo_agent"])
            self.assertFalse((state_less_dir / "state.json").exists())
            self.assertTrue(result.timed_out)
            self.assertEqual(result.changed_agents, [])
            self.assertEqual(result.message, "No subagents to wait for.")

    def test_send_message_queues_without_reply_and_followup_task_triggers_reply(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td) / "temp" / "demo"
            task_dir.mkdir(parents=True)
            manager = SubagentManager(root_dir=td)

            manager.send_message("demo", "queued only", author="/root")
            self.assertFalse((task_dir / "reply.txt").exists())
            manager.followup_task("demo", "run next", author="/root")

            mailbox_rows = [
                json.loads(line)
                for line in (task_dir / "mailbox.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([row["content"] for row in mailbox_rows], ["queued only", "run next"])
            self.assertEqual([row["trigger_turn"] for row in mailbox_rows], [False, True])
            self.assertTrue(all(row["author"] == "/root" for row in mailbox_rows))
            self.assertEqual((task_dir / "reply.txt").read_text(encoding="utf-8"), "run next")


if __name__ == "__main__":
    unittest.main()
