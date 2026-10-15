import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from agentmain import _teammate_idle_hook  # noqa: E402
from subagent_tasks import SubagentTaskStore  # noqa: E402
from subagent_team import team_name_for  # noqa: E402


class TeammateIdleHookTest(unittest.TestCase):
    def _task_dir(self, td, name="agent_1", session="sess-12345678"):
        task_dir = Path(td) / "temp" / name
        task_dir.mkdir(parents=True)
        (task_dir / "state.json").write_text(
            json.dumps({"parent_session_id": session}), encoding="utf-8"
        )
        return task_dir

    def test_claims_the_next_task_and_writes_the_cc_prompt(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = self._task_dir(td)
            store = SubagentTaskStore(td, list_id=team_name_for("sess-12345678"))
            task = store.create_task("write the report", "include numbers")

            claimed = _teammate_idle_hook(str(task_dir), "agent_1")

            self.assertTrue(claimed)
            prompt = (task_dir / "reply.txt").read_text(encoding="utf-8")
            self.assertIn("Complete all open tasks", prompt)
            self.assertIn("write the report", prompt)
            self.assertEqual(store.get_task(task["id"])["owner"], "/root/agent_1")
            self.assertEqual(store.get_task(task["id"])["status"], "in_progress")

    def test_reports_idle_to_the_lead_when_nothing_is_claimable(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = self._task_dir(td, name="agent_2")

            claimed = _teammate_idle_hook(str(task_dir), "agent_2")

            self.assertFalse(claimed)
            self.assertFalse((task_dir / "reply.txt").exists())
            inbox = Path(td) / "temp" / "subagents" / "inbox.jsonl"
            rows = [json.loads(line) for line in inbox.read_text(encoding="utf-8").splitlines() if line.strip()]
            self.assertTrue(any(row.get("type") == "idle_notification" for row in rows))

    def test_does_not_report_idle_when_a_lead_dm_already_went_out(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = self._task_dir(td, name="agent_3")
            (task_dir / "events.jsonl").write_text(
                json.dumps({"type": "turn_completed"}) + "\n" + json.dumps({"type": "lead_message"}) + "\n",
                encoding="utf-8",
            )

            _teammate_idle_hook(str(task_dir), "agent_3")

            inbox = Path(td) / "temp" / "subagents" / "inbox.jsonl"
            if inbox.exists():
                rows = [json.loads(line) for line in inbox.read_text(encoding="utf-8").splitlines() if line.strip()]
                self.assertFalse(any(row.get("type") == "idle_notification" for row in rows))


if __name__ == "__main__":
    unittest.main()
