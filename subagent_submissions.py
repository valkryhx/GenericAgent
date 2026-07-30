from __future__ import annotations

import json
from pathlib import Path

from subagent_state import atomic_write_lines, cross_process_lock, now_iso


MAX_ROWS = 2000


class SubagentSubmissionLog:
    """Identity for control-plane ops, so a replayed call does not execute twice.

    Measured: calling ``followup_task`` twice for the same logical submission queued two
    trigger_turn rows, i.e. the child ran the task twice — the mailbox already had a
    ``message_id`` dedup branch, but nothing above it ever passed an id. Same shape as Codex's
    ``Submission { id, op, trace }`` (`codex-rs/protocol/src/protocol.rs`): the op carries its own
    identity rather than the caller hoping it only arrives once.

    Deliberately *not* a general audit trail: it stores just enough to answer "did this
    submission already run, and what did it return", capped at MAX_ROWS so an agent tree that
    runs for hours does not grow an unbounded file.
    """

    def __init__(self, log_dir, *, max_rows=MAX_ROWS):
        self.log_dir = Path(log_dir)
        self.path = self.log_dir / "submissions.jsonl"
        self.lock_path = self.log_dir / "submissions.jsonl.lock"
        self.max_rows = int(max_rows)

    @staticmethod
    def normalize_id(submission_id):
        """Blank means "don't dedup me" — recording that would collide unrelated calls."""
        text = str(submission_id or "").strip()
        return text or None

    def record(self, submission_id, *, op, target=None, result=None):
        submission_id = self.normalize_id(submission_id)
        if submission_id is None:
            return None
        with self._locked():
            rows = self._read_rows()
            existing = next((row for row in rows if row.get("submission_id") == submission_id), None)
            if existing is not None:
                # The first execution is the one that really happened.
                return existing
            row = {
                "schema_version": 1,
                "submission_id": submission_id,
                "op": str(op),
                "target": str(target) if target is not None else None,
                "result": _serializable(result),
                "created_at": now_iso(),
            }
            rows.append(row)
            self._write_rows(rows[-self.max_rows :])
            return row

    def find(self, submission_id):
        submission_id = self.normalize_id(submission_id)
        if submission_id is None:
            return None
        for row in self._read_rows():
            if row.get("submission_id") == submission_id:
                return row
        return None

    def _locked(self):
        self.log_dir.mkdir(parents=True, exist_ok=True)
        return cross_process_lock(self.lock_path)

    def _read_rows(self):
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        rows = []
        for line in lines:
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            if row.get("submission_id"):
                rows.append(row)
        return rows

    def _write_rows(self, rows):
        atomic_write_lines(
            self.path,
            [json.dumps(row, ensure_ascii=False, separators=(",", ":")) for row in rows],
        )


def _serializable(value):
    """Keep the row writable no matter what the op returned.

    Dedup is the point; the stored payload is a convenience. A result that cannot be JSON-encoded
    must not cost us the row, or the replay would re-execute.
    """
    try:
        json.dumps(value, ensure_ascii=False)
        return value
    except (TypeError, ValueError):
        return {"repr": repr(value)[:2000]}
