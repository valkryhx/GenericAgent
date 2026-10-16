import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from genericagent.core.loop import exhaust  # noqa: E402
from genericagent.core.handler import GenericAgentHandler  # noqa: E402
from genericagent.agents.manager import SubagentManager  # noqa: E402


class FakeParent:
    def __init__(self):
        self.llmclient = None
        self.verbose = False
        self.task_dir = None
        self.llm_no = 0


class GaTaskToolsTest(unittest.TestCase):
    def make_handler(self, root):
        handler = GenericAgentHandler(FakeParent(), last_history=[], cwd=str(Path(root) / "temp"))
        handler.subagent_manager = SubagentManager(root_dir=root)
        return handler

    def test_task_tools_roundtrip(self):
        with tempfile.TemporaryDirectory() as td:
            handler = self.make_handler(td)

            created = exhaust(handler.do_task_create({"subject": "write parser", "description": "spec.md"}, response=None))
            self.assertEqual(created.data["status"], "success")
            task_id = created.data["task"]["id"]

            listed = exhaust(handler.do_task_list({}, response=None))
            self.assertEqual([t["id"] for t in listed.data["tasks"]], [task_id])

            fetched = exhaust(handler.do_task_get({"taskId": task_id}, response=None))
            self.assertEqual(fetched.data["task"]["subject"], "write parser")

            claimed = exhaust(handler.do_task_update({"taskId": task_id, "owner": "alice", "status": "in_progress"}, response=None))
            self.assertTrue(claimed.data["success"])

            rejected = exhaust(handler.do_task_update({"taskId": task_id, "owner": "bob"}, response=None))
            self.assertFalse(rejected.data["success"])
            self.assertEqual(rejected.data["reason"], "already_claimed")

            done = exhaust(handler.do_task_update({"taskId": task_id, "status": "completed"}, response=None))
            self.assertTrue(done.data["success"])

    def test_task_create_requires_subject(self):
        with tempfile.TemporaryDirectory() as td:
            handler = self.make_handler(td)
            outcome = exhaust(handler.do_task_create({}, response=None))
            self.assertEqual(outcome.data["status"], "error")


if __name__ == "__main__":
    unittest.main()
