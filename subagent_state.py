import hashlib
import json
import os
import threading
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = 1


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def atomic_write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    os.replace(tmp, path)


def read_json_or_none(path):
    path = Path(path)
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def append_jsonl_event(path, event):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = dict(event)
    row.setdefault("schema_version", SCHEMA_VERSION)
    row.setdefault("ts", now_iso())
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def consume_mailbox_trigger(path):
    path = Path(path)
    if not path.exists():
        return None
    rows = []
    selected = None
    changed = False
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    for line in lines:
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            rows.append({"schema_version": SCHEMA_VERSION, "type": "malformed", "raw": line})
            continue
        if selected is None and row.get("trigger_turn") and not row.get("consumed_at"):
            row["consumed_at"] = now_iso()
            selected = row
            changed = True
        rows.append(row)
    if changed:
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            for row in rows:
                f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        os.replace(tmp, path)
    if selected is None:
        return None
    return selected.get("content") or ""


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
