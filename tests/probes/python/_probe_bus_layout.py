"""Where do a real run's subagent events and notifications actually land?

Walks the product data root for one session and prints every event-bus directory it
finds, with the event types each one holds. This is read-only: it inspects files a
previous run left behind.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

DATA_ROOT = Path("C:/Users/Administrator/AppData/Local/GenericAgent/projects")
REPORT = Path("D:/tmp/ga_probe_bus_layout.txt")


def rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            out.append({"__bad__": line[:120]})
    return out


def main(argv: list[str]) -> int:
    since = argv[1] if len(argv) > 1 else ""
    lines: list[str] = []
    out = lines.append

    for bus_dir in sorted(DATA_ROOT.rglob("subagents")):
        events_path = bus_dir / "events.jsonl"
        notes_path = bus_dir / "notifications.jsonl"
        if not events_path.exists() and not notes_path.exists():
            continue
        newest = max(
            (p.stat().st_mtime for p in (events_path, notes_path) if p.exists()), default=0
        )
        import datetime

        stamp = datetime.datetime.fromtimestamp(newest).strftime("%Y-%m-%d %H:%M:%S")
        if since and stamp < since:
            continue
        out(f"=== {bus_dir}")
        out(f"    last write: {stamp}")
        ev = rows(events_path)
        out(f"    events.jsonl rows: {len(ev)}  types: {sorted({r.get('type') for r in ev if r.get('type')})}")
        nt = rows(notes_path)
        out(f"    notifications.jsonl rows: {len(nt)}")
        for r in nt:
            out(f"      seq={r.get('event_seq')} type={r.get('type')} agent={r.get('agent_path')} at={r.get('created_at')}")
        cur = bus_dir / "notification_cursor.json"
        out(f"    cursor: {cur.read_text(encoding='utf-8').strip() if cur.exists() else '<none>'}")
        out("")

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
