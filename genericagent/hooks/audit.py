"""Append-only hook audit log and bounded output persistence."""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from genericagent import paths
from genericagent.agents.state import append_jsonl_event
from genericagent.core.redaction import sanitize


class HookAudit:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root or (paths.TEMP_DIR / "hooks"))
        self.output_dir = self.root / "outputs"

    def record(self, result, *, payload: dict[str, Any] | None = None) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        data = sanitize(result.to_audit_dict())
        max_chars = 20_000
        for key in ("stdout", "stderr"):
            value = str(data.get(key) or "")
            if len(value) > max_chars:
                self.output_dir.mkdir(parents=True, exist_ok=True)
                path = self.output_dir / f"{result.hook_run_id}.{key}.txt"
                path.write_text(value, encoding="utf-8", newline="\n")
                data[key] = value[:max_chars] + f"\n[persisted: {path}]"
        data["input"] = sanitize(payload or {})
        day = datetime.now().strftime("%Y%m%d")
        append_jsonl_event(self.root / f"events-{day}.jsonl", data)

    def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        files = sorted(self.root.glob("events-*.jsonl"), reverse=True)
        rows: list[dict[str, Any]] = []
        for path in files:
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
            except OSError:
                continue
            for line in reversed(lines):
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
                if len(rows) >= max(1, int(limit)):
                    return list(reversed(rows))
        return list(reversed(rows))
