import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from agent_loop import exhaust  # noqa: E402
from ga import GenericAgentHandler, get_global_memory  # noqa: E402
from subagent_manager import SubagentManager  # noqa: E402
from subagent_state import atomic_write_json  # noqa: E402


class FakeParent:
    def __init__(self):
        backend = type(
            "Backend",
            (),
            {
                "history": [
                    {"role": "user", "content": "parent context"},
                    {"role": "assistant", "content": "parent answer"},
                ]
            },
        )()
        self.llmclient = type("Client", (), {"backend": backend})()
        self.verbose = False
        self.task_dir = None
        self.llm_no = 1
        self.session_id = "parent_session"


class GaSubagentToolsTest(unittest.TestCase):
    def make_handler(self, root, **manager_kwargs):
        handler = GenericAgentHandler(FakeParent(), last_history=[], cwd=str(Path(root) / "temp"))
        handler.subagent_manager = SubagentManager(root_dir=root, **manager_kwargs)
        return handler

    def test_spawn_agent_tool_defaults_to_context_fork_and_keeps_prompt_out_of_command(self):
        with tempfile.TemporaryDirectory() as td:
            calls = []

            class FakeProcess:
                pid = 7654

            def fake_popen(cmd, **kwargs):
                calls.append((cmd, kwargs))
                return FakeProcess()

            handler = self.make_handler(td, popen=fake_popen, python_executable="python-test")

            outcome = exhaust(
                handler.do_spawn_agent(
                    {
                        "task_name": "context_worker",
                        "message": "inspect inherited context",
                    },
                    response=None,
                )
            )

            task_dir = Path(td) / "temp" / "context_worker"
            self.assertEqual(outcome.data["status"], "started")
            self.assertEqual(outcome.data["fork_turns"], "all")
            self.assertEqual((task_dir / "input.txt").read_text(encoding="utf-8"), "inspect inherited context")
            self.assertEqual(
                json.loads((task_dir / "_history.json").read_text(encoding="utf-8")),
                FakeParent().llmclient.backend.history,
            )
            cmd, kwargs = calls[0]
            self.assertEqual(cmd[0], "python-test")
            self.assertIn("--task_root", cmd)
            self.assertIn(str(Path(td)), cmd)
            self.assertNotIn("inspect inherited context", cmd)
            self.assertEqual(Path(kwargs["cwd"]), Path(td))

    def test_global_memory_distinguishes_tool_cwd_from_workspace_root(self):
        prompt = get_global_memory()

        self.assertIn("tool scratch/output dir", prompt)
        self.assertIn("workspace root =", prompt)
        self.assertIn("README.md", prompt)

    def test_wait_agent_tool_returns_already_completed_final_output(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td) / "temp" / "done_worker"
            task_dir.mkdir(parents=True)
            output_path = task_dir / "output.txt"
            output_path.write_text("finished work\n\n[ROUND END]\n", encoding="utf-8")
            atomic_write_json(
                task_dir / "state.json",
                {
                    "schema_version": 1,
                    "task_name": "done_worker",
                    "agent_path": "/root/done_worker",
                    "pid": 123,
                    "round": 0,
                    "turn_status": "running",
                    "process_status": "alive",
                    "output_path": str(output_path),
                },
            )
            handler = self.make_handler(td, process_exists=lambda pid: True, sleep=lambda _: None)

            outcome = exhaust(
                handler.do_wait_agent(
                    {
                        "target": "done_worker",
                        "timeout_seconds": 0,
                    },
                    response=None,
                )
            )

            self.assertEqual(outcome.data["status"], "changed")
            self.assertEqual(outcome.data["agents"][0]["turn_status"], "completed")
            self.assertEqual(outcome.data["agents"][0]["process_status"], "waiting_reply")
            self.assertEqual(outcome.data["agents"][0]["final_output"], "finished work")

    def test_wait_agent_empty_targets_falls_back_to_target(self):
        with tempfile.TemporaryDirectory() as td:
            temp_root = Path(td) / "temp"
            (temp_root / "ga-real-e2e-tmp").mkdir(parents=True)
            (temp_root / "ga-real-e2e-tmp" / "state.json").write_text("{}", encoding="utf-8")
            task_dir = temp_root / "done_worker"
            task_dir.mkdir()
            output_path = task_dir / "output.txt"
            output_path.write_text("finished work\n\n[ROUND END]\n", encoding="utf-8")
            atomic_write_json(
                task_dir / "state.json",
                {
                    "schema_version": 1,
                    "task_name": "done_worker",
                    "agent_path": "/root/done_worker",
                    "pid": 123,
                    "round": 0,
                    "turn_status": "running",
                    "process_status": "alive",
                    "output_path": str(output_path),
                },
            )
            handler = self.make_handler(td, process_exists=lambda pid: True, sleep=lambda _: None)

            outcome = exhaust(
                handler.do_wait_agent(
                    {
                        "target": "/root/done_worker",
                        "targets": [],
                        "timeout_seconds": 0,
                    },
                    response=None,
                )
            )

            self.assertEqual(outcome.data["status"], "changed")
            self.assertEqual([agent["task_name"] for agent in outcome.data["agents"]], ["done_worker"])
            self.assertEqual(outcome.data["agents"][0]["final_output"], "finished work")

    def test_message_followup_and_interrupt_tools_write_mailbox_reply_and_stop_file(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td) / "temp" / "mail_worker"
            task_dir.mkdir(parents=True)
            atomic_write_json(
                task_dir / "state.json",
                {
                    "schema_version": 1,
                    "task_name": "mail_worker",
                    "agent_path": "/root/mail_worker",
                    "pid": 999,
                    "round": 0,
                    "turn_status": "running",
                    "process_status": "alive",
                },
            )
            handler = self.make_handler(td, process_exists=lambda pid: True, sleep=lambda _: None)

            send_outcome = exhaust(handler.do_send_message({"target": "mail_worker", "message": "note only"}, None))
            followup_outcome = exhaust(handler.do_followup_task({"target": "mail_worker", "message": "run next"}, None))
            interrupt_outcome = exhaust(handler.do_interrupt_agent({"target": "mail_worker", "reason": "test"}, None))

            self.assertEqual(send_outcome.data["status"], "queued")
            self.assertEqual(followup_outcome.data["status"], "queued")
            rows = [
                json.loads(line)
                for line in (task_dir / "mailbox.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([row["content"] for row in rows], ["note only", "run next"])
            self.assertEqual([row["trigger_turn"] for row in rows], [False, True])
            self.assertEqual((task_dir / "reply.txt").read_text(encoding="utf-8"), "run next")
            self.assertEqual(interrupt_outcome.data["status"], "interrupt_requested")
            self.assertEqual((task_dir / "_stop").read_text(encoding="utf-8"), "test")

    def test_tool_schemas_include_subagent_tools(self):
        expected = {"spawn_agent", "list_agents", "wait_agent", "send_message", "followup_task", "interrupt_agent"}
        for schema_name in ["tools_schema.json", "tools_schema_cn.json"]:
            with self.subTest(schema_name=schema_name):
                raw = json.loads((REPO_ROOT / "assets" / schema_name).read_text(encoding="utf-8"))
                names = {item["function"]["name"] for item in raw}
                self.assertTrue(expected.issubset(names))


if __name__ == "__main__":
    unittest.main()
