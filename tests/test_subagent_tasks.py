import sys
import tempfile
import threading
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from genericagent.agents.tasks import SubagentTaskStore, next_task_prompt  # noqa: E402


class SubagentTaskStoreTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.store = SubagentTaskStore(self._td.name, list_id="team-demo")

    def tearDown(self):
        self._td.cleanup()

    def test_create_is_pending_unowned(self):
        task = self.store.create_task("write the parser", "see spec.md")
        self.assertEqual(task["status"], "pending")
        self.assertIsNone(task["owner"])
        self.assertEqual(task["blocks"], [])
        self.assertEqual(task["blockedBy"], [])
        self.assertEqual(self.store.get_task(task["id"])["subject"], "write the parser")

    def test_ids_are_monotonic_and_not_reused_after_delete(self):
        a = self.store.create_task("a")
        b = self.store.create_task("b")
        self.store.update_task(a["id"], status="deleted")
        c = self.store.create_task("c")
        self.assertNotEqual(c["id"], a["id"])
        self.assertEqual([t["id"] for t in self.store.list_tasks()], [b["id"], c["id"]])

    def test_claim_rejects_second_owner(self):
        task = self.store.create_task("shared")
        first = self.store.claim_task(task["id"], "alice", start=True)
        self.assertTrue(first["success"])
        self.assertEqual(first["task"]["status"], "in_progress")
        second = self.store.claim_task(task["id"], "bob")
        self.assertFalse(second["success"])
        self.assertEqual(second["reason"], "already_claimed")

    def test_claim_rejects_blocked_and_completed(self):
        blocker = self.store.create_task("blocker")
        blocked = self.store.create_task("blocked")
        self.store.update_task(blocked["id"], add_blocked_by=[blocker["id"]])
        result = self.store.claim_task(blocked["id"], "alice")
        self.assertEqual(result["reason"], "blocked")
        self.store.update_task(blocker["id"], status="completed")
        # Completing the blocker releases the dependency.
        self.assertTrue(self.store.claim_task(blocked["id"], "alice")["success"])

        done = self.store.create_task("done")
        self.store.update_task(done["id"], status="completed")
        self.assertEqual(self.store.claim_task(done["id"], "alice")["reason"], "already_resolved")

    def test_claim_with_busy_check(self):
        a = self.store.create_task("a")
        b = self.store.create_task("b")
        self.assertTrue(self.store.claim_task(a["id"], "alice", start=True)["success"])
        result = self.store.claim_task(b["id"], "alice", check_busy=True)
        self.assertFalse(result["success"])
        self.assertEqual(result["reason"], "agent_busy")
        self.assertEqual(result["busyWithTasks"], [a["id"]])

    def test_list_hides_internal_and_filters_completed_blockers(self):
        blocker = self.store.create_task("blocker")
        blocked = self.store.create_task("blocked")
        self.store.update_task(blocked["id"], add_blocked_by=[blocker["id"]])
        hidden = self.store.create_task("hidden", metadata={"_internal": True})
        self.assertNotIn(hidden["id"], [t["id"] for t in self.store.list_tasks()])
        summary = next(t for t in self.store.list_tasks() if t["id"] == blocked["id"])
        self.assertEqual(summary["blockedBy"], [blocker["id"]])
        self.store.update_task(blocker["id"], status="completed")
        summary = next(t for t in self.store.list_tasks() if t["id"] == blocked["id"])
        self.assertEqual(summary["blockedBy"], [])

    def test_blocks_edges_are_bidirectional(self):
        a = self.store.create_task("a")
        b = self.store.create_task("b")
        self.store.update_task(a["id"], add_blocks=[b["id"]])
        self.assertIn(b["id"], self.store.get_task(a["id"])["blocks"])
        self.assertIn(a["id"], self.store.get_task(b["id"])["blockedBy"])

    def test_delete_detaches_edges(self):
        a = self.store.create_task("a")
        b = self.store.create_task("b")
        self.store.update_task(a["id"], add_blocks=[b["id"]])
        self.store.update_task(b["id"], status="deleted")
        self.assertEqual(self.store.get_task(a["id"])["blocks"], [])

    def test_find_claimable_skips_owned_and_blocked(self):
        a = self.store.create_task("a")
        b = self.store.create_task("b")
        c = self.store.create_task("c")
        self.store.claim_task(a["id"], "alice", start=True)
        self.store.update_task(b["id"], add_blocked_by=[a["id"]])
        self.assertEqual(self.store.find_claimable("bob")["id"], c["id"])
        self.store.update_task(c["id"], status="completed")
        self.assertIsNone(self.store.find_claimable("bob"))

    def test_reclaim_owner_resets_to_pending(self):
        a = self.store.create_task("a")
        b = self.store.create_task("b")
        self.store.claim_task(a["id"], "alice", start=True)
        self.store.claim_task(b["id"], "alice", start=True)
        self.store.update_task(b["id"], status="completed")
        reclaimed = self.store.reclaim_owner("alice")
        self.assertEqual([r["id"] for r in reclaimed], [a["id"]])
        self.assertEqual(self.store.get_task(a["id"])["status"], "pending")
        self.assertIsNone(self.store.get_task(a["id"])["owner"])
        self.assertEqual(self.store.get_task(b["id"])["status"], "completed")

    def test_concurrent_claim_single_winner(self):
        task = self.store.create_task("race")
        winners = []

        def worker(name):
            if self.store.claim_task(task["id"], name, start=True)["success"]:
                winners.append(name)

        threads = [threading.Thread(target=worker, args=(f"w{i}",)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(len(winners), 1)


class NextTaskPromptTest(unittest.TestCase):
    def test_prompt_names_the_task_and_includes_the_description(self):
        prompt = next_task_prompt({"id": "2", "subject": "audit the log", "description": "line by line"})

        self.assertIn("task #2", prompt)
        self.assertIn("audit the log", prompt)
        self.assertIn("line by line", prompt)

    def test_prompt_without_a_description_has_no_trailing_blank(self):
        prompt = next_task_prompt({"id": "1", "subject": "just do it"})

        self.assertEqual(prompt, "Complete all open tasks. Start with task #1: just do it")

    def test_empty_task_yields_an_empty_prompt(self):
        self.assertEqual(next_task_prompt(None), "")


class CompleteOwnedInProgressTest(unittest.TestCase):
    def test_completes_only_this_owner_in_progress_tasks(self):
        with tempfile.TemporaryDirectory() as td:
            store = SubagentTaskStore(td, list_id="team-a")
            mine = store.create_task("mine", "")
            other = store.create_task("other", "")
            pending = store.create_task("untouched", "")
            store.claim_task(mine["id"], "/root/a", start=True)
            store.claim_task(other["id"], "/root/b", start=True)

            done = store.complete_owned_in_progress("/root/a")

            self.assertEqual([mine["id"]], [item["id"] for item in done])
            self.assertEqual(store.get_task(mine["id"])["status"], "completed")
            self.assertEqual(store.get_task(other["id"])["status"], "in_progress")
            self.assertEqual(store.get_task(pending["id"])["status"], "pending")
            self.assertEqual(store.complete_owned_in_progress("/root/a"), [])

    def test_completed_tasks_are_not_reclaimed(self):
        with tempfile.TemporaryDirectory() as td:
            store = SubagentTaskStore(td, list_id="team-a")
            task = store.create_task("done work", "")
            store.claim_task(task["id"], "/root/a", start=True)
            store.complete_owned_in_progress("/root/a")

            reclaimed = store.reclaim_owner("/root/a")

            self.assertEqual(reclaimed, [])
            self.assertEqual(store.get_task(task["id"])["status"], "completed")


if __name__ == "__main__":
    unittest.main()
