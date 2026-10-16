"""Cross-session inbound gate (CC ``m1356``): accept / hold / refuse.

CC evidence (v2.1.283):

- Policy lattice ``f = {accept:0, hold:1, refuse:2}``; a message is admitted,
  parked in a bounded ``held[]`` buffer (``F = 100``, oldest evicted as
  ``expired`` when full), or refused.
- Receipt vocabulary: ``accepted | held | refused | expired | dropped |
  delivered | denied``.
- ``A(reason)`` releases every held message when the local mode changes;
  ``OQe(msgId, action)`` approves/denies a single held message; shutdown settles
  the buffer as ``expired`` after a ``N = 750ms`` grace.
- Origins are classified (``_bt``) into ``peer | host-injected | coordinator |
  ungated``; a coordinator notification (``task-notification`` with
  ``subkind == "peer-send-message"``) only yields to an explicit ``refuse``, and
  a ``selfSent`` peer message is always accepted.
- The host records each inbound write in ``mailbox.ledger`` and skips a message
  whose exact entry was already written (``m0184 wrote()``) -- idempotent inbound.

GA has no cross-machine peer transport, so the socket / kill-switch layers are
dropped; the local policy, the bounded hold buffer, the receipt state machine and
the idempotent-write check are kept. 【复刻时暂时不需要参考 claudecode 云端会话的实现】
"""

from __future__ import annotations

import json
import os
from pathlib import Path

from subagent_state import cross_process_lock, now_iso


# CC `m1356` f = {accept:0, hold:1, refuse:2}
POLICY_RANK = {"accept": 0, "hold": 1, "refuse": 2}
# CC `m1356` F = 100
HOLD_BUFFER = 100
# CC `m1356` N = 750 (ms)
SETTLE_GRACE_MS = 750

RECEIPT_ACCEPTED = "accepted"
RECEIPT_HELD = "held"
RECEIPT_REFUSED = "refused"
RECEIPT_EXPIRED = "expired"
RECEIPT_DROPPED = "dropped"
RECEIPT_DELIVERED = "delivered"
RECEIPT_DENIED = "denied"


def normalize_policy(value):
    """Map a setting/env value onto the policy lattice (unknown -> accept)."""
    text = str(value or "").strip().lower()
    return text if text in POLICY_RANK else "accept"


def resolve_policy(explicit=None):
    """CC ``I()`` precedence, reduced to GA's single config knob.

    Explicit argument > ``GA_CROSS_SESSION_INBOUND`` env > ``accept``.
    """
    if explicit is not None:
        return normalize_policy(explicit)
    return normalize_policy(os.environ.get("GA_CROSS_SESSION_INBOUND", "accept"))


class InboundGate:
    """Bounded accept/hold/refuse gate for messages arriving at an agent."""

    def __init__(self, base_dir, *, policy=None, hold_buffer=HOLD_BUFFER, ledger=None, receipts=None):
        self.base_dir = Path(base_dir)
        self.policy = resolve_policy(policy)
        self.hold_buffer = int(hold_buffer)
        self.ledger = ledger
        self.receipts = receipts
        self.held = []
        self.shutting_down = False
        self._announced = {}
        self.receipt_path = self.base_dir / "inbound_receipts.jsonl"
        self.receipt_lock = self.receipt_path.with_name("inbound_receipts.jsonl.lock")

    # -- policy ---------------------------------------------------------------
    def set_policy(self, policy):
        """Change the gate policy and release anything now admitted (CC ``ubt``)."""
        self.policy = resolve_policy(policy)
        return self.release("mode-changed")

    def origin_kind(self, msg):
        """Classify the message origin (CC ``_bt``)."""
        origin = (msg or {}).get("origin")
        if not origin:
            return "ungated"
        if isinstance(origin, str):
            kind = origin
            host_injected = False
            subkind = None
        else:
            kind = origin.get("kind")
            host_injected = bool(origin.get("hostInjected"))
            subkind = origin.get("subkind")
        if kind == "peer":
            return "host-injected" if host_injected else "peer"
        if kind == "task-notification" and subkind == "peer-send-message":
            return "coordinator"
        return "ungated"

    def decide(self, msg):
        """Resolve the policy for one message (CC ``H`` / ``P`` / ``E`` / ``T``)."""
        kind = self.origin_kind(msg)
        if kind == "ungated":
            return "accept"
        if kind == "coordinator":
            return "refuse" if self.policy == "refuse" else "accept"
        # peer / host-injected
        if (msg or {}).get("self_sent"):
            return "accept"
        return self.policy

    # -- apply ----------------------------------------------------------------
    def apply(self, msg):
        """Admit / hold / refuse one message. Returns ``accept|held|refused|duplicate``."""
        if self._already_written(msg):
            self._receipt(msg, RECEIPT_DROPPED, drop_reason="duplicate")
            return "duplicate"
        policy = self.decide(msg)
        if policy == "accept":
            self._receipt(msg, RECEIPT_ACCEPTED)
            self._record_written(msg)
            return "accept"
        if policy == "refuse":
            self._receipt(msg, RECEIPT_REFUSED)
            return "refused"
        # hold
        if self.shutting_down:
            self._receipt(msg, RECEIPT_EXPIRED)
            return "refused"
        if len(self.held) >= self.hold_buffer:
            oldest = self.held.pop(0)
            self._receipt(oldest, RECEIPT_EXPIRED, drop_reason="buffer-full")
        self.held.append(msg)
        self._announced[self._msg_id(msg)] = "policy-hold"
        self._receipt(msg, RECEIPT_HELD)
        return "held"

    # -- release / approve / deny / settle -----------------------------------
    def release(self, reason="mode-changed"):
        """Re-decide every held message; return the ones now admitted (CC ``A``)."""
        admitted, still_held = [], []
        for msg in self.held:
            policy = self.decide(msg)
            if policy == "accept":
                admitted.append(msg)
                self._record_written(msg)
                self._receipt(msg, RECEIPT_DELIVERED, release_reason=reason)
            elif policy == "refuse":
                self._receipt(msg, RECEIPT_REFUSED, drop_reason=reason)
            else:
                still_held.append(msg)
        self.held = still_held
        return admitted

    def approve(self, msg_id):
        """Approve one held message (CC ``OQe(..., "approve")``)."""
        msg = self._take_held(msg_id)
        if msg is None:
            return "gone"
        if self.decide(msg) == "refuse":
            self._receipt(msg, RECEIPT_REFUSED, drop_reason="approved-but-refused")
            return "dropped"
        self._record_written(msg)
        self._receipt(msg, RECEIPT_DELIVERED, release_reason="approved")
        return "delivered"

    def deny(self, msg_id, reason=RECEIPT_DENIED):
        """Deny / expire one held message (CC ``OQe`` non-approve branch)."""
        msg = self._take_held(msg_id)
        if msg is None:
            return "gone"
        state = reason if reason in (RECEIPT_DENIED, RECEIPT_EXPIRED) else RECEIPT_DENIED
        self._receipt(msg, state, drop_reason=state)
        return "dropped"

    def settle_shutdown(self):
        """Settle every held message as ``expired`` (CC ``mbt``). Returns the count."""
        self.shutting_down = True
        count = len(self.held)
        for msg in self.held:
            self._receipt(msg, RECEIPT_EXPIRED, drop_reason="shutdown")
        self.held = []
        return count

    def held_count(self):
        return len(self.held)

    def held_ids(self):
        return [self._msg_id(msg) for msg in self.held]

    def snapshot(self):
        return {
            "policy": self.policy,
            "held": list(self.held),
            "held_count": len(self.held),
            "shutting_down": self.shutting_down,
        }

    def reset(self):
        """Clear the held buffer and transient state (CC ``o.reset()``)."""
        self.held = []
        self._announced = {}
        self.shutting_down = False

    # -- internals -----------------------------------------------------------
    def _take_held(self, msg_id):
        target = str(msg_id or "")
        for index, msg in enumerate(self.held):
            if self._msg_id(msg) == target:
                return self.held.pop(index)
        return None

    def _entry(self, msg):
        """Canonical ledger entry for a message (CC ``m0184`` entry fields)."""
        return {
            "msg_id": self._msg_id(msg),
            "from": (msg or {}).get("author"),
            "timestamp": (msg or {}).get("timestamp") or (msg or {}).get("created_at"),
            "text": (msg or {}).get("content"),
            "summary": (msg or {}).get("summary"),
            "color": (msg or {}).get("color"),
            "from_plugin": (msg or {}).get("from_plugin"),
        }

    def _already_written(self, msg):
        if self.ledger is None:
            return False
        writer = (msg or {}).get("author")
        try:
            return bool(self.ledger.wrote(writer, self._entry(msg)))
        except Exception:
            return False

    def _record_written(self, msg):
        if self.ledger is None:
            return
        try:
            self.ledger.record((msg or {}).get("author"), self._entry(msg))
        except Exception:
            pass

    def _receipt(self, msg, state, **extra):
        """Append a receipt row and (best-effort) advance the sender's ledger."""
        msg_id = self._msg_id(msg)
        row = {
            "msg_id": msg_id,
            "from": (msg or {}).get("author"),
            "to": (msg or {}).get("recipient"),
            "state": str(state),
            "at": now_iso(),
        }
        row.update(extra)
        try:
            self.base_dir.mkdir(parents=True, exist_ok=True)
            with cross_process_lock(self.receipt_lock):
                with self.receipt_path.open("a", encoding="utf-8", newline="\n") as handle:
                    handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        except OSError:
            pass
        if self.receipts is not None and msg_id:
            try:
                self.receipts.resolve(msg_id, str(state))
            except Exception:
                pass
        return row

    def receipts_log(self):
        try:
            lines = self.receipt_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        rows = []
        for line in lines:
            if not line.strip():
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return rows

    @staticmethod
    def _msg_id(msg):
        msg = msg or {}
        return msg.get("message_id") or msg.get("msg_id")
