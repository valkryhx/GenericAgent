from __future__ import annotations

import json
from pathlib import Path

from genericagent.agents.state import atomic_write_lines, cross_process_lock, now_iso


QUEUE_ONLY = "queue_only"
TRIGGER_TURN = "trigger_turn"

# CC `m1575` Sge: how many times the read mark is retried before we deliver unmarked.
MARK_READ_RETRIES = 3


class SubagentMailbox:
    def __init__(self, mailbox_path):
        self.path = Path(mailbox_path)
        self.lock_path = self.path.with_name(f"{self.path.name}.lock")

    def enqueue(
        self,
        content,
        *,
        author,
        recipient,
        delivery_mode=QUEUE_ONLY,
        priority="normal",
        message_id=None,
        reply_to=None,
        source_tool=None,
        other_recipients=None,
        provenance=None,
    ):
        delivery_mode = TRIGGER_TURN if str(delivery_mode) == TRIGGER_TURN else QUEUE_ONLY
        # The parent enqueues while the child consumes, and both rewrite the whole
        # file, so the read-modify-write cycle has to be serialized across processes.
        with self._locked():
            rows = self._read_rows()
            if message_id and any(row.get("message_id") == message_id for row in rows):
                return next(row for row in rows if row.get("message_id") == message_id)
            message_id = message_id or self._new_message_id(rows)
            row = {
                "schema_version": 1,
                "message_id": message_id,
                "id": message_id,
                "author": author,
                "recipient": recipient,
                # Who else was told the same thing. Codex carries this in
                # InterAgentCommunication.other_recipients so a recipient can avoid duplicating
                # a peer's work; without it every agent assumes it is the only one who knows.
                "other_recipients": list(other_recipients or []),
                "content": str(content),
                "delivery_mode": delivery_mode,
                "trigger_turn": delivery_mode == TRIGGER_TURN,
                "priority": priority,
                "created_at": now_iso(),
                "consumed_at": None,
                "acknowledged_at": None,
                "reply_to": reply_to,
                "source_tool": source_tool,
                # Delivery-receipt state machine (CC m0188): queued -> delivered ->
                # consumed, or held/expired/dropped/forged for the non-happy paths.
                "state": "queued",
                "digest": None,
            }
            if provenance is not None:
                row["digest"] = provenance.record(
                    author=author, recipient=recipient, content=content, message_id=message_id
                )
            rows.append(row)
            self._write_rows(rows)
            return row

    def annotate(self, message_id, *, other_recipients=None):
        """Attach peer context to an already-queued row; returns the row or None.

        Separate from enqueue because a multicast only knows the full peer list after every
        recipient has been resolved, and the annotation must never be able to fail the delivery
        that already happened.
        """
        if not message_id:
            return None
        with self._locked():
            rows = self._read_rows()
            updated = None
            for index, row in enumerate(rows):
                if row.get("message_id") != message_id:
                    continue
                row = dict(row)
                if other_recipients is not None:
                    row["other_recipients"] = list(other_recipients)
                rows[index] = row
                updated = row
                break
            if updated is None:
                return None
            self._write_rows(rows)
            return updated

    def consume_trigger_turn(self, provenance=None, roster=None, mark_read_retries=MARK_READ_RETRIES):
        """Consume the next trigger-turn message bundle.

        ``provenance`` (a ``ProvenanceLedger``) turns on anti-forgery: rows whose
        digest is not in the ledger are marked ``forged`` and never consumed
        (CC ``m0300 Wae``). ``mark_read_retries`` mirrors CC ``m1575``: the read mark
        is retried, and if it still fails the message is delivered unmarked and its
        id is remembered so a later consume does not deliver it twice.
        """
        with self._locked():
            rows = self._read_rows()
            verdicts = {}
            if provenance is not None:
                digests = provenance.digests()
                for row in rows:
                    verdict = provenance.verify(row, roster, digests=digests)
                    verdicts[row.get("message_id")] = verdict
                    if verdict == "forged":
                        row["state"] = "forged"
            unmarked = self._unmarked_ids()
            unconsumed = [
                row for row in rows
                if not row.get("consumed_at")
                and verdicts.get(row.get("message_id")) != "forged"
                and row.get("message_id") not in unmarked
            ]
            trigger_index = None
            for index, row in enumerate(unconsumed):
                if row.get("delivery_mode") == TRIGGER_TURN or row.get("trigger_turn"):
                    trigger_index = index
                    break
            if trigger_index is None:
                if any(v == "forged" for v in verdicts.values()):
                    self._write_rows(rows)
                return None
            selected = unconsumed[: trigger_index + 1]
            selected_ids = {row.get("message_id") or row.get("id") for row in selected}
            consumed_at = now_iso()
            updated = []
            for row in rows:
                row = dict(row)
                row_id = row.get("message_id") or row.get("id")
                if row_id in selected_ids:
                    row["consumed_at"] = consumed_at
                    row["acknowledged_at"] = consumed_at
                    row["state"] = "consumed"
                updated.append(row)
            marked = self._write_rows_with_retry(updated, mark_read_retries)
            if not marked:
                self._record_unmarked(sorted(selected_ids))
            return {
                "content": "\n\n".join(str(row.get("content") or "") for row in selected),
                "messages": selected,
                "consumed_at": consumed_at,
                "marked": marked,
            }

    def mark_delivered(self, message_ids):
        ids = {str(item) for item in (message_ids or ()) if item}
        if not ids:
            return 0
        with self._locked():
            rows = self._read_rows()
            changed = 0
            for row in rows:
                if row.get("message_id") in ids and row.get("state") == "queued":
                    row["state"] = "delivered"
                    changed += 1
            if changed:
                self._write_rows(rows)
            return changed

    def pending_trigger_ids(self):
        """Ids of unconsumed trigger-turn messages (used before shutdown)."""
        return [
            row.get("message_id")
            for row in self._read_rows()
            if not row.get("consumed_at")
            and (row.get("delivery_mode") == TRIGGER_TURN or row.get("trigger_turn"))
        ]

    def _write_rows_with_retry(self, rows, retries):
        attempts = max(1, int(retries or 1))
        for _ in range(attempts):
            try:
                self._write_rows(rows)
                return True
            except OSError:
                continue
        return False

    def _unmarked_path(self):
        return self.path.with_name(self.path.name + ".unmarked.json")

    def _unmarked_ids(self):
        try:
            data = json.loads(self._unmarked_path().read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return set()
        return set(str(item) for item in (data.get("ids") or []))

    def _record_unmarked(self, message_ids):
        ids = self._unmarked_ids()
        ids.update(str(item) for item in (message_ids or ()))
        try:
            self._unmarked_path().write_text(
                json.dumps({"ids": sorted(ids)}, ensure_ascii=False), encoding="utf-8"
            )
        except OSError:
            pass

    def _locked(self):
        return cross_process_lock(self.lock_path)

    def _new_message_id(self, rows):
        # Row count alone collides with any id already inside the msg_%06d namespace,
        # and the dedup branch above would then swallow the new message as a duplicate.
        taken = {row.get("message_id") for row in rows}
        candidate = len(rows) + 1
        while f"msg_{candidate:06d}" in taken:
            candidate += 1
        return f"msg_{candidate:06d}"

    def _read_rows(self):
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        rows = []
        seen = set()
        for line in lines:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            message_id = row.get("message_id") or row.get("id")
            if not message_id or message_id in seen:
                continue
            row["message_id"] = message_id
            row.setdefault("id", message_id)
            row.setdefault("delivery_mode", TRIGGER_TURN if row.get("trigger_turn") else QUEUE_ONLY)
            row.setdefault("trigger_turn", row.get("delivery_mode") == TRIGGER_TURN)
            row.setdefault("consumed_at", None)
            row.setdefault("acknowledged_at", None)
            row.setdefault("other_recipients", [])
            row.setdefault("state", "consumed" if row.get("consumed_at") else "queued")
            seen.add(message_id)
            rows.append(row)
        return rows

    def _write_rows(self, rows):
        # Full-file rewrite under an atomic replace, so a reader that is not holding
        # the lock still only ever sees a complete mailbox.
        atomic_write_lines(
            self.path,
            [json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in rows],
        )
