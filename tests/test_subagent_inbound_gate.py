import os
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from subagent_inbound_gate import (  # noqa: E402
    HOLD_BUFFER,
    InboundGate,
    normalize_policy,
    resolve_policy,
)
from subagent_swarm import MailboxLedger  # noqa: E402


def _msg(msg_id, *, author="/root/agent_1", content="hi", origin=None, self_sent=False):
    origin = {"kind": "peer"} if origin is None else origin
    msg = {
        "message_id": msg_id,
        "author": author,
        "recipient": "/root",
        "content": content,
        "timestamp": "2026-01-01T00:00:00Z",
    }
    if origin is not None:
        msg["origin"] = origin
    if self_sent:
        msg["self_sent"] = True
    return msg


class PolicyTest(unittest.TestCase):
    def test_normalize_unknown_is_accept(self):
        self.assertEqual(normalize_policy("hold"), "hold")
        self.assertEqual(normalize_policy("REFUSE"), "refuse")
        self.assertEqual(normalize_policy("bogus"), "accept")
        self.assertEqual(normalize_policy(None), "accept")

    def test_resolve_prefers_explicit_then_env(self):
        self.assertEqual(resolve_policy("hold"), "hold")
        old = os.environ.get("GA_CROSS_SESSION_INBOUND")
        try:
            os.environ["GA_CROSS_SESSION_INBOUND"] = "refuse"
            self.assertEqual(resolve_policy(), "refuse")
        finally:
            if old is None:
                os.environ.pop("GA_CROSS_SESSION_INBOUND", None)
            else:
                os.environ["GA_CROSS_SESSION_INBOUND"] = old

    def test_default_hold_buffer(self):
        self.assertEqual(HOLD_BUFFER, 100)


class OriginKindTest(unittest.TestCase):
    def test_classification(self):
        with tempfile.TemporaryDirectory() as td:
            gate = InboundGate(td)
            self.assertEqual(gate.origin_kind({}), "ungated")
            self.assertEqual(gate.origin_kind(_msg("m", origin={"kind": "peer"})), "peer")
            self.assertEqual(gate.origin_kind(_msg("m", origin={"kind": "peer", "hostInjected": True})), "host-injected")
            self.assertEqual(
                gate.origin_kind(_msg("m", origin={"kind": "task-notification", "subkind": "peer-send-message"})),
                "coordinator",
            )


class AcceptPolicyTest(unittest.TestCase):
    def test_default_accepts_and_records_receipt(self):
        with tempfile.TemporaryDirectory() as td:
            gate = InboundGate(td)
            self.assertEqual(gate.apply(_msg("m1")), "accept")
            states = [row["state"] for row in gate.receipts_log()]
            self.assertEqual(states, ["accepted"])

    def test_self_sent_always_accepted(self):
        with tempfile.TemporaryDirectory() as td:
            gate = InboundGate(td, policy="refuse")
            self.assertEqual(gate.apply(_msg("m1", self_sent=True)), "accept")

    def test_coordinator_only_refused_by_explicit_refuse(self):
        coord = _msg("m1", origin={"kind": "task-notification", "subkind": "peer-send-message"})
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(InboundGate(td, policy="hold").apply(coord), "accept")
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(InboundGate(td, policy="refuse").apply(coord), "refused")


class HoldPolicyTest(unittest.TestCase):
    def test_hold_parks_message_and_receipts_held(self):
        with tempfile.TemporaryDirectory() as td:
            gate = InboundGate(td, policy="hold")
            self.assertEqual(gate.apply(_msg("m1")), "held")
            self.assertEqual(gate.held_count(), 1)
            self.assertEqual(gate.held_ids(), ["m1"])
            self.assertEqual(gate.receipts_log()[-1]["state"], "held")

    def test_full_buffer_evicts_oldest_as_expired(self):
        with tempfile.TemporaryDirectory() as td:
            gate = InboundGate(td, policy="hold", hold_buffer=2)
            gate.apply(_msg("m1"))
            gate.apply(_msg("m2"))
            gate.apply(_msg("m3"))
            self.assertEqual(gate.held_ids(), ["m2", "m3"])
            states = [row["state"] for row in gate.receipts_log()]
            self.assertIn("expired", states)

    def test_release_after_policy_change_admits(self):
        with tempfile.TemporaryDirectory() as td:
            gate = InboundGate(td, policy="hold")
            gate.apply(_msg("m1"))
            admitted = gate.set_policy("accept")
            self.assertEqual([m["message_id"] for m in admitted], ["m1"])
            self.assertEqual(gate.held_count(), 0)
            self.assertEqual(gate.receipts_log()[-1]["state"], "delivered")

    def test_release_keeps_still_held(self):
        with tempfile.TemporaryDirectory() as td:
            gate = InboundGate(td, policy="hold")
            gate.apply(_msg("m1"))
            self.assertEqual(gate.release("mode-changed"), [])
            self.assertEqual(gate.held_count(), 1)

    def test_approve_delivers(self):
        with tempfile.TemporaryDirectory() as td:
            gate = InboundGate(td, policy="hold")
            gate.apply(_msg("m1"))
            self.assertEqual(gate.approve("m1"), "delivered")
            self.assertEqual(gate.held_count(), 0)
            self.assertEqual(gate.receipts_log()[-1]["state"], "delivered")

    def test_approve_unknown_is_gone(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(InboundGate(td, policy="hold").approve("nope"), "gone")

    def test_deny_drops(self):
        with tempfile.TemporaryDirectory() as td:
            gate = InboundGate(td, policy="hold")
            gate.apply(_msg("m1"))
            self.assertEqual(gate.deny("m1"), "dropped")
            self.assertEqual(gate.receipts_log()[-1]["state"], "denied")

    def test_settle_shutdown_expires_all(self):
        with tempfile.TemporaryDirectory() as td:
            gate = InboundGate(td, policy="hold")
            gate.apply(_msg("m1"))
            gate.apply(_msg("m2"))
            self.assertEqual(gate.settle_shutdown(), 2)
            self.assertEqual(gate.held_count(), 0)
            self.assertTrue(gate.shutting_down)

    def test_shutdown_refuses_a_late_hold(self):
        with tempfile.TemporaryDirectory() as td:
            gate = InboundGate(td, policy="hold")
            gate.settle_shutdown()
            self.assertEqual(gate.apply(_msg("m1")), "refused")
            self.assertEqual(gate.receipts_log()[-1]["state"], "expired")


class RefusePolicyTest(unittest.TestCase):
    def test_refuse_receipts_refused(self):
        with tempfile.TemporaryDirectory() as td:
            gate = InboundGate(td, policy="refuse")
            self.assertEqual(gate.apply(_msg("m1")), "refused")
            self.assertEqual(gate.receipts_log()[-1]["state"], "refused")


class IdempotentLedgerTest(unittest.TestCase):
    def test_duplicate_entry_is_dropped(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = MailboxLedger(td)
            gate = InboundGate(td, ledger=ledger)
            self.assertEqual(gate.apply(_msg("m1")), "accept")
            self.assertEqual(gate.apply(_msg("m1")), "duplicate")

    def test_same_id_different_content_is_not_duplicate(self):
        with tempfile.TemporaryDirectory() as td:
            ledger = MailboxLedger(td)
            gate = InboundGate(td, ledger=ledger)
            gate.apply(_msg("m1", content="a"))
            self.assertEqual(gate.apply(_msg("m1", content="b")), "accept")


class ReceiptLedgerWiringTest(unittest.TestCase):
    def test_receipt_resolves_the_senders_outstanding_send(self):
        from subagent_receipts import SubagentSendLedger

        with tempfile.TemporaryDirectory() as td:
            receipts = SubagentSendLedger(td)
            receipts.record_send("m1", to="/root")
            gate = InboundGate(td, receipts=receipts)
            gate.apply(_msg("m1"))
            self.assertEqual(receipts.snapshot()["sends"][0]["state"], "accepted")


if __name__ == "__main__":
    unittest.main()
