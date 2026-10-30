"""Local-first goal lifecycle telemetry (Stage F, phase 5, S2).

Mirrors Claude Code's goal telemetry intent (m0017 session markers + m0671
``tengu_goal_*`` events) but deliberately keeps it LOCAL: one JSON object per
line appended to ``<data_root>/logs/goal_events.jsonl``. Nothing leaves the
machine by default, and the payload never contains the goal condition text or
any other conversation content -- only counts, durations and an event name.

Best-effort by contract: every recorder swallows its own errors so a bad disk
never breaks a goal transition. ``GA_GOAL_TELEMETRY=off`` disables all writes.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

# Event names (GA simplification, aligned with CC tengu_goal_* set).
EVENT_SET = "goal_set"
EVENT_CLEARED = "goal_cleared"
EVENT_ACHIEVED = "goal_achieved"
EVENT_FAILED = "goal_failed"
EVENT_MET = "goal_met"
EVENT_PROPOSED = "goal_proposed"
EVENT_PROPOSAL_DECIDED = "goal_proposal_decided"

# Default is local; only an explicit "off"/"0"/"false" disables writing.
_DISABLED_VALUES = frozenset({"off", "0", "false", "no", "disabled", ""})


def telemetry_enabled() -> bool:
    raw = os.environ.get("GA_GOAL_TELEMETRY")
    if raw is None:
        return True  # default: local
    return str(raw).strip().lower() not in _DISABLED_VALUES


def _logs_dir(data_root: Any = None) -> Path:
    if data_root:
        return Path(str(data_root)) / "logs"
    from genericagent import paths

    return Path(str(paths.data_root())) / "logs"


def record(event: str, *, data_root: Any = None, **fields: Any) -> dict | None:
    """Append one event. Returns the written row, or None when disabled.

    ``reason`` is never stored: only ``reason_len``. Free-form text fields an
    agent might pass are dropped so private content cannot leak into the log.
    """
    if not telemetry_enabled():
        return None
    row: dict[str, Any] = {"event": str(event), "at": round(time.time(), 3)}
    for key in ("origin", "iterations", "durationMs", "tokens", "reason_len",
                "met", "outcome", "ask_user"):
        value = fields.get(key)
        if value is not None:
            row[key] = value
    if "reason" in fields and "reason_len" not in row:
        row["reason_len"] = len(str(fields.get("reason") or ""))
    try:
        path = _logs_dir(data_root) / "goal_events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        return row
    except Exception:
        return None


def read_events(data_root: Any = None) -> list[dict]:
    """Test helper: parse the JSONL file (empty list when absent/corrupt lines)."""
    path = _logs_dir(data_root) / "goal_events.jsonl"
    if not path.is_file():
        return []
    rows: list[dict] = []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return rows
    return rows
