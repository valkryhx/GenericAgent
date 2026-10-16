import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from genericagent.agents.swarm import (  # noqa: E402
    MAILBOX_LEDGER_CAP,
    MailboxLedger,
    SubagentSwarm,
    TaskListState,
    get_swarm,
    mailbox_entry_digest,
    reset_swarm_cache,
)


def _entry(msg_id, text="hello", ts="2026-01-01T00:00:00Z"):
    return {"msg_id": msg_id, "from": "/root/agent_1", "timestamp": ts, "text": text}


class MailboxEntryDigestTest(unittest.TestCase):
    def test_digest_is_stable_and_text_sensitive(self):
        self.assertEqual(mailbox_entry_digest(_entry("m1")), mailbox_entry_digest(_entry("m1")))
        self.assertNotEqual(mailbox_entry_digest(_entry("m1")), mailbox_entry_digest(_entry("m1", text="other")))

    def test_missing_field_hashes_as_null_not_empty(self):
        a = mailbox_entry_digest({"msg_id": "m1", "text": "x"})
        b = mailbox_entry_digest({"msg_id": "m1", "text": "x", "summary": ""})
        self.assertNotEqual(a, b)


class MailboxLedgerTest(unittest.TestCase):
    def test_record_then_wrote_reports_true(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = MailboxLedger(td)
            entry = _entry("m1")
            ledger.record("/root/agent_1", entry)
            self.assertTrue(ledger.wrote("/root/agent_1", entry))

    def test_wrote_false_for_same_id_different_content(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = MailboxLedger(td)
            ledger.record("/root/agent_1", _entry("m1"))
            self.assertFalse(ledger.wrote("/root/agent_1", _entry("m1", text="changed")))

    def test_wrote_false_for_unknown_writer(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = MailboxLedger(td)
            ledger.record("/root/agent_1", _entry("m1"))
            self.assertFalse(ledger.wrote("/root/agent_2", _entry("m1")))

    def test_record_without_msg_id_is_ignored(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = MailboxLedger(td)
            self.assertIsNone(ledger.record("/root/agent_1", {"from": "x"}))
            self.assertFalse(ledger.wrote("/root/agent_1", {"from": "x"}))

    def test_cap_evicts_oldest_fifo(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = MailboxLedger(td, cap=3)
            for index in range(5):
                ledger.record("/root/agent_1", _entry("m%d" % index))
            entries = ledger.entries("/root/agent_1")
            self.assertEqual(len(entries), 3)
            self.assertNotIn("m0", entries)
            self.assertIn("m4", entries)

    def test_retire_and_forget(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = MailboxLedger(td)
            ledger.record("/root/agent_1", _entry("m1"))
            ledger.record("/root/agent_1", _entry("m2"))
            ledger.retire("/root/agent_1", ["m1"])
            self.assertEqual(set(ledger.entries("/root/agent_1")), {"m2"})
            ledger.forget("/root/agent_1")
            self.assertEqual(ledger.entries("/root/agent_1"), {})

    def test_retain_pins_writer_against_eviction(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = MailboxLedger(td, cap=2)
            ledger.retain("/root/agent_1", [])
            for index in range(5):
                ledger.record("/root/agent_1", _entry("m%d" % index))
            self.assertEqual(len(ledger.entries("/root/agent_1")), 5)

    def test_default_cap_constant(self):
        self.assertEqual(MAILBOX_LEDGER_CAP, 2048)


class TaskListStateTest(unittest.TestCase):
    def test_mark_updated_increments(self):
        with tempfile.TemporaryDirectory() as td:
            state = TaskListState(td)
            self.assertEqual(state.updated_seq(), 0)
            self.assertEqual(state.mark_updated(), 1)
            self.assertEqual(state.mark_updated(team_name="team-a"), 2)
            self.assertEqual(state.updated_seq(), 2)
            self.assertEqual(state.leader_team_name, "team-a")

    def test_reset_emits_when_a_leader_was_set(self):
        with tempfile.TemporaryDirectory() as td:
            state = TaskListState(td)
            state.leader_team_name = "team-a"
            state.reset()
            self.assertIsNone(state.leader_team_name)
            self.assertEqual(state.updated_seq(), 1)


class SubagentSwarmTest(unittest.TestCase):
    def test_wires_all_slots(self):
        with tempfile.TemporaryDirectory() as td:
            swarm = SubagentSwarm(td)
            self.assertIsNotNone(swarm.receipts)
            self.assertIsNotNone(swarm.mailbox_ledger)
            self.assertIsNotNone(swarm.task_list)
            self.assertIsNotNone(swarm.inbound)
            self.assertEqual(swarm.permissions["pending"], {})

    def test_reset_clears_transient_state(self):
        with tempfile.TemporaryDirectory() as td:
            swarm = SubagentSwarm(td, policy="hold")
            swarm.inbound.apply({"message_id": "m1", "author": "/root/a", "content": "x"})
            swarm.permissions["pending"]["k"] = 1
            swarm.reset()
            self.assertEqual(swarm.inbound.held_count(), 0)
            self.assertEqual(swarm.permissions["pending"], {})

    def test_get_swarm_is_a_per_dir_singleton(self):
        with tempfile.TemporaryDirectory() as td:
            reset_swarm_cache()
            self.assertIs(get_swarm(td), get_swarm(td))
            with tempfile.TemporaryDirectory() as td2:
                self.assertIsNot(get_swarm(td), get_swarm(td2))
            reset_swarm_cache()


if __name__ == "__main__":
    unittest.main()
