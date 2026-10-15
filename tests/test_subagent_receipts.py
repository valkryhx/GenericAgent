import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from subagent_receipts import (  # noqa: E402
    ProvenanceLedger,
    SubagentSendLedger,
    build_roster,
    message_digest,
)


class SendLedgerTest(unittest.TestCase):
    def test_record_and_resolve_a_receipt(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = SubagentSendLedger(td)
            ledger.record_send("msg_1", to="/root/agent_1", digest="abc")

            resolved = ledger.resolve("msg_1", "consumed")

            self.assertEqual(resolved["destination"], "/root/agent_1")
            self.assertFalse(resolved["was_held"])
            self.assertEqual(ledger.snapshot()["sends"][0]["state"], "consumed")

    def test_resolve_reports_whether_the_send_was_held(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = SubagentSendLedger(td)
            ledger.record_send("msg_1", to="/root/agent_1")
            ledger.resolve("msg_1", "held")

            resolved = ledger.resolve("msg_1", "consumed")

            self.assertTrue(resolved["was_held"])

    def test_reap_drops_pending_receipts_for_dead_recipients(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = SubagentSendLedger(td)
            ledger.record_send("msg_1", to="/root/dead")
            ledger.record_send("msg_2", to="/root/alive")
            ledger.resolve("msg_1", "held")

            result = ledger.reap({"/root/dead"})

            self.assertEqual(result["dropped"], 1)
            self.assertEqual(result["were_held"], 1)
            self.assertEqual([row["message_id"] for row in ledger.snapshot()["sends"]], ["msg_2"])

    def test_outstanding_is_bounded_fifo(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = SubagentSendLedger(td, max_outstanding=5)
            for index in range(8):
                ledger.record_send("msg_%d" % index, to="/root/a")
                ledger.resolve("msg_%d" % index, "consumed")

            rows = ledger.snapshot()["sends"]

            self.assertEqual(len(rows), 5)
            self.assertEqual([row["message_id"] for row in rows], ["msg_3", "msg_4", "msg_5", "msg_6", "msg_7"])

    def test_trim_keeps_pending_over_terminal(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = SubagentSendLedger(td, max_outstanding=2)
            ledger.record_send("done_1", to="/root/a")
            ledger.resolve("done_1", "consumed")
            ledger.record_send("pending_1", to="/root/a")
            ledger.record_send("pending_2", to="/root/a")

            rows = ledger.snapshot()["sends"]

            self.assertEqual(len(rows), 2)
            self.assertEqual({row["message_id"] for row in rows}, {"pending_1", "pending_2"})


class ProvenanceLedgerTest(unittest.TestCase):
    def test_recorded_message_verifies(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = ProvenanceLedger(td)
            ledger.record(author="/root", recipient="/root/a", content="hi", message_id="msg_1")
            row = {"author": "/root", "recipient": "/root/a", "content": "hi", "message_id": "msg_1"}

            self.assertEqual(ledger.verify(row, {"/root"}), "verified")

    def test_tampered_message_from_a_known_author_is_forged(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = ProvenanceLedger(td)
            ledger.record(author="/root", recipient="/root/a", content="hi", message_id="msg_1")
            tampered = {"author": "/root", "recipient": "/root/a", "content": "evil", "message_id": "msg_1"}

            self.assertEqual(ledger.verify(tampered, {"/root"}), "forged")

    def test_unknown_author_without_a_record_is_unverifiable(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = ProvenanceLedger(td)
            row = {"author": "/root/stranger", "recipient": "/root/a", "content": "hi", "message_id": "msg_1"}

            self.assertEqual(ledger.verify(row, {"/root"}), "unverifiable")

    def test_digest_is_stable_and_content_sensitive(self):
        self.assertEqual(
            message_digest("/root", "/root/a", "hi", "msg_1"),
            message_digest("/root", "/root/a", "hi", "msg_1"),
        )
        self.assertNotEqual(
            message_digest("/root", "/root/a", "hi", "msg_1"),
            message_digest("/root", "/root/a", "bye", "msg_1"),
        )

    def test_build_roster_always_includes_root(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertIn("/root", build_roster(td))


if __name__ == "__main__":
    unittest.main()
