from __future__ import annotations

import json
from pathlib import Path

from subagent_state import atomic_write_lines, cross_process_lock, now_iso


QUEUE_ONLY = "queue_only"
TRIGGER_TURN = "trigger_turn"


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
                "content": str(content),
                "delivery_mode": delivery_mode,
                "trigger_turn": delivery_mode == TRIGGER_TURN,
                "priority": priority,
                "created_at": now_iso(),
                "consumed_at": None,
                "acknowledged_at": None,
                "reply_to": reply_to,
                "source_tool": source_tool,
            }
            rows.append(row)
            self._write_rows(rows)
            return row

    def consume_trigger_turn(self):
        with self._locked():
            rows = self._read_rows()
            unconsumed = [row for row in rows if not row.get("consumed_at")]
            trigger_index = None
            for index, row in enumerate(unconsumed):
                if row.get("delivery_mode") == TRIGGER_TURN or row.get("trigger_turn"):
                    trigger_index = index
                    break
            if trigger_index is None:
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
                updated.append(row)
            self._write_rows(updated)
            return {
                "content": "\n\n".join(str(row.get("content") or "") for row in selected),
                "messages": selected,
                "consumed_at": consumed_at,
            }

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
