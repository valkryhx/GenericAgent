"""Delivery receipts + message provenance for the durable subagent file bus.

CC evidence (v2.1.283):

- ``m0188`` ``Pe(...)`` records ``{msgId, to}`` in ``receipts.outstandingSends``
  (cap ``be=200``, FIFO) and drives a receipt state machine ``XAr(msgId, state)``:
  a send moves ``outstanding`` -> ``awaitingTerminal`` when held, and out of both
  tables when it reaches a terminal state; ``JAr(deadIds)`` reaps the pending
  receipts of a sender that died (``{dropped, wereHeld}``).
- ``m1356`` inbound gate: ``accept``/``hold``/``refuse``; held messages buffer to
  ``F=100`` and the oldest is evicted as ``expired``; shutdown settles held as
  ``expired``.
- ``m0300 Wae(unread, inboxPath, {roster})`` provenance: a message is ``verified``
  only when the process ledger recorded writing it; otherwise, if the sender is in
  the roster it is ``forged``, else ``unverifiable``. Only non-``forged`` messages
  are consumed.

GA runs on a durable file bus (the message is already persisted before the
recipient ever reads it), so we do not need CC's socket transport checks. What GA
was missing is exactly the two bookkeeping layers: a receipt state machine so the
sender can see whether a message was delivered/consumed, and a provenance ledger
so a recipient can tell a genuine message from a hand-written row.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from subagent_state import atomic_write_json, cross_process_lock, now_iso, read_json_or_none


# CC `m0188` be=200
DEFAULT_MAX_OUTSTANDING = 200
# CC `m1356` F=100
DEFAULT_HOLD_BUFFER = 100

TERMINAL_STATES = frozenset({"consumed", "dropped", "expired", "failed"})
PENDING_STATES = frozenset({"sent", "delivered", "held"})


def message_digest(author, recipient, content, message_id):
    """Stable digest of a mailbox message, used as its provenance fingerprint."""
    payload = "\n".join(
        (str(message_id or ""), str(author or ""), str(recipient or ""), str(content or ""))
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class SubagentSendLedger:
    """Outstanding-send receipts for one sender (parent or teammate).

    File: ``<base_dir>/receipts.json``. Bounded FIFO so a long-lived sender does
    not grow without limit; ``held`` rows survive until they reach a terminal state.
    """

    def __init__(self, base_dir, *, max_outstanding=DEFAULT_MAX_OUTSTANDING):
        self.base_dir = Path(base_dir)
        self.path = self.base_dir / "receipts.json"
        self.lock_path = self.path.with_name("receipts.json.lock")
        self.max_outstanding = int(max_outstanding)

    def record_send(self, message_id, *, to, digest=None, state="sent"):
        if not message_id:
            return None
        with cross_process_lock(self.lock_path):
            data = self._read()
            sends = [row for row in data.get("sends") or [] if row.get("message_id") != message_id]
            row = {
                "message_id": str(message_id),
                "to": str(to or ""),
                "state": str(state),
                "digest": digest,
                "at": now_iso(),
            }
            sends.append(row)
            data["sends"] = self._trim(sends)
            atomic_write_json(self.path, data)
            return row

    def resolve(self, message_id, state):
        """Advance a receipt. Returns ``{destination, was_held}`` or ``None``."""
        if not message_id:
            return None
        with cross_process_lock(self.lock_path):
            data = self._read()
            sends = list(data.get("sends") or [])
            found = None
            for row in sends:
                if row.get("message_id") == message_id:
                    found = row
                    break
            if found is None:
                return None
            was_held = found.get("state") == "held"
            found["state"] = str(state)
            found["resolved_at"] = now_iso()
            data["sends"] = self._trim(sends)
            atomic_write_json(self.path, data)
            return {"destination": found.get("to"), "was_held": was_held}

    def reap(self, dead_recipients):
        """Drop pending receipts aimed at recipients that are gone (CC ``JAr``)."""
        dead = {str(item) for item in (dead_recipients or ())}
        if not dead:
            return {"dropped": 0, "were_held": 0}
        with cross_process_lock(self.lock_path):
            data = self._read()
            kept, dropped, were_held = [], 0, 0
            for row in data.get("sends") or []:
                if row.get("to") in dead and row.get("state") in PENDING_STATES:
                    dropped += 1
                    if row.get("state") == "held":
                        were_held += 1
                    continue
                kept.append(row)
            data["sends"] = kept
            atomic_write_json(self.path, data)
            return {"dropped": dropped, "were_held": were_held}

    def snapshot(self):
        return self._read()

    def _trim(self, sends):
        if len(sends) <= self.max_outstanding:
            return sends
        # Keep every non-terminal row (they still owe a receipt); evict the oldest
        # terminal rows first, then the oldest pending rows (FIFO).
        overflow = len(sends) - self.max_outstanding
        evict = set()
        for index, row in enumerate(sends):
            if overflow <= 0:
                break
            if row.get("state") not in PENDING_STATES:
                evict.add(index)
                overflow -= 1
        for index in range(len(sends)):
            if overflow <= 0:
                break
            if index not in evict:
                evict.add(index)
                overflow -= 1
        return [row for index, row in enumerate(sends) if index not in evict]

    def _read(self):
        data = read_json_or_none(self.path)
        if not isinstance(data, dict):
            return {"sends": []}
        if not isinstance(data.get("sends"), list):
            data["sends"] = []
        return data


class ProvenanceLedger:
    """Append-only ledger of message digests written through the mailbox.

    File: ``<base_dir>/provenance.jsonl``. Verification follows CC ``m0300 Wae``:
    ``verified`` when the ledger recorded this exact message; ``forged`` when the
    claimed author is a known team member but no ledger record exists; otherwise
    ``unverifiable``.
    """

    def __init__(self, base_dir):
        self.base_dir = Path(base_dir)
        self.path = self.base_dir / "provenance.jsonl"
        self.lock_path = self.path.with_name("provenance.jsonl.lock")

    def record(self, *, author, recipient, content, message_id):
        digest = message_digest(author, recipient, content, message_id)
        row = {
            "digest": digest,
            "message_id": str(message_id or ""),
            "to": str(recipient or ""),
            "at": now_iso(),
        }
        with cross_process_lock(self.lock_path):
            self.base_dir.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8", newline="\n") as handle:
                handle.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        return digest

    def digests(self):
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return set()
        out = set()
        for line in lines:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("digest"):
                out.add(row["digest"])
        return out

    def verify(self, row, roster=None, digests=None):
        digest = row.get("digest") or message_digest(
            row.get("author"), row.get("recipient"), row.get("content"), row.get("message_id")
        )
        known = self.digests() if digests is None else digests
        if digest in known:
            return "verified"
        if str(row.get("author") or "") in {str(item) for item in (roster or ())}:
            return "forged"
        return "unverifiable"

def build_roster(temp_dir):
    """Agent paths currently known to the registry, for provenance verification."""
    paths = {"/root"}
    try:
        from subagent_registry import SubagentRegistry

        registry = SubagentRegistry(Path(temp_dir) / "subagents")
        for state in registry.list_agents(include_closed=True):
            if getattr(state, "agent_path", None):
                paths.add(str(state.agent_path))
    except Exception:
        pass
    return paths
