import hashlib
import json
import os
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path


SCHEMA_VERSION = 1
# os.replace on Windows fails while any other handle is open on the destination, so a
# concurrent reader (CLI inspection, a monitor tailing the file) can bounce a writer.
# Readers open-read-close in microseconds, so backing off past the read burst is enough;
# the budget is long so a reader can never turn into a lost write.
_WINDOWS_REPLACE_RETRY_DELAYS = (0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.3, 0.5, 0.8)


def now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@contextmanager
def cross_process_lock(lock_path):
    """Serialize read-modify-write cycles on a durable file across processes.

    Shared by the event bus and the mailbox so both use one lock implementation
    instead of drifting. Windows has no flock, so msvcrt.locking spins on the
    first byte; POSIX uses a blocking flock.
    """
    lock_path = Path(lock_path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+", encoding="utf-8") as lock_file:
        if os.name == "nt":
            import msvcrt

            while True:
                try:
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                    break
                except OSError:
                    time.sleep(0.01)
            try:
                yield
            finally:
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _replace_file(src, dst):
    delays = _WINDOWS_REPLACE_RETRY_DELAYS if os.name == "nt" else ()
    for attempt in range(len(delays) + 1):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt >= len(delays):
                raise
            time.sleep(delays[attempt])


def atomic_write_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.write("\n")
    _replace_file(tmp, path)


def read_json_or_none(path):
    path = Path(path)
    if not path.exists():
        return None
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def atomic_write_lines(path, lines):
    """Replace a whole text file in one step so readers never see a partial rewrite.

    Same tmp + os.replace trick as atomic_write_json, but for JSONL-style bodies
    that are rewritten in full (the mailbox) rather than appended to.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as f:
        for line in lines:
            f.write(line if line.endswith("\n") else line + "\n")
    _replace_file(tmp, path)


def read_text_retrying(path):
    """Read a file that another process may be replacing right now.

    `_replace_file` retries when a reader's open handle blocks `os.replace`; this is the mirror
    image, because on Windows a reader that opens *during* the replace gets PermissionError rather
    than a torn file. Same delay budget, so the two sides back off over the same window.
    """
    path = Path(path)
    delays = _WINDOWS_REPLACE_RETRY_DELAYS if os.name == "nt" else ()
    for attempt in range(len(delays) + 1):
        try:
            return path.read_bytes().decode("utf-8")
        except PermissionError:
            if attempt >= len(delays):
                raise
            time.sleep(delays[attempt])


def read_json_retrying(path):
    """read_text_retrying + json.loads, propagating a decode error instead of hiding it.

    Deliberately not read_json_or_none: a caller asking "was this run killed?" must not read a
    corrupt row as "no kill on disk".
    """
    return json.loads(read_text_retrying(path))


def atomic_write_text(path, text):
    """Replace a whole text file in one step, byte-for-byte as given.

    Sibling of atomic_write_lines for content that is read back and compared verbatim (the
    workflow `script.js` becomes `run.script`), so it must not append a trailing newline.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(text)
    _replace_file(tmp, path)


def append_jsonl_event(path, event):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = dict(event)
    row.setdefault("schema_version", SCHEMA_VERSION)
    row.setdefault("ts", now_iso())
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def parent_inbox_path_for_task_dir(task_dir):
    return Path(task_dir).parent / "subagents" / "inbox.jsonl"


def append_parent_inbox_event(task_dir, event):
    row = dict(event)
    task_name = row.get("task_name") or Path(task_dir).name
    row.setdefault("type", "subagent_update")
    row.setdefault("author", f"/root/{task_name}")
    row.setdefault("recipient", "/root")
    row.setdefault("task_name", task_name)
    row.setdefault("agent_path", f"/root/{task_name}")
    row.setdefault("task_dir", str(task_dir))
    append_jsonl_event(parent_inbox_path_for_task_dir(task_dir), row)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
