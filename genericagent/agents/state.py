import errno
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
_LOCAL_LOCKS = {}
_LOCAL_LOCKS_GUARD = threading.Lock()

# Windows resolves paths through the Win32 layer, which caps them at MAX_PATH (260) unless
# the machine has LongPathsEnabled=1 (then 32767). An installed library cannot require that
# registry change, so the only honest thing to do is say so when a write hits the cap.
#
# This matters because the atomic writers append `.<pid>.<tid>.tmp` to the target name: a
# perfectly legal target can fail purely because the *temp* name crossed the limit, and the
# raw error for that is `FileNotFoundError: [Errno 2] No such file or directory: '...tmp'`
# -- which reads as "the caller passed a bad path" or "the directory is missing". Two real
# causes hide behind it: over-long target directory (WinError 3 / 206 from mkdir) and
# over-long temp name (ENOENT from open). Both are recorded in
# tests/test_write_path_diagnostics.py and docs/20261116-CC319长路径处理与GA对比.md.
_WINDOWS_MAX_PATH = 260


class PathTooLongError(OSError):
    """A write failed because the resulting path exceeds the platform limit.

    Subclasses OSError so every existing ``except OSError`` caller keeps working; the
    difference is that the message names the actual cause and the numbers involved.
    """


def _is_path_too_long(error: OSError, attempted: str) -> bool:
    """True when `error` is the name-too-long condition rather than a genuine miss.

    Windows reports this inconsistently, and the most common case carries NO distinguishing
    error code at all:
      * `open()` on an over-long temp name -> FileNotFoundError, errno=2, winerror=None
      * `mkdir` badly past the limit      -> WinError 3 "系统找不到指定的路径"
      * `mkdir` just past the limit       -> WinError 206 "文件名或扩展名太长"

    The first cannot be told apart from a genuinely missing directory by errno, so the
    attempted path's length is what decides: on Windows without LongPathsEnabled a path of
    260+ chars can never resolve, so whichever call failed, length is the real cause.
    A short path keeps its ordinary error.
    """
    if error.errno == errno.ENAMETOOLONG:
        return True
    if getattr(error, "winerror", None) in (206, 3):
        return True
    return len(attempted) >= _WINDOWS_MAX_PATH


def _raise_path_too_long(path, attempted: str, error: OSError, limit: int) -> None:
    """Re-raise `error` as a PathTooLongError that explains what to do about it."""
    parent_len = len(str(path.parent))
    # Do NOT repeat the path in the message: OSError appends the filename argument to
    # str(), so naming it twice reads as two different paths.
    raised = PathTooLongError(
        error.errno,
        "path too long for this platform: the file to write is %d chars, limit is %d "
        "(MAX_PATH on Windows, raised only by a machine-wide registry change no installed "
        "library may require). The atomic write adds a '.<pid>.<tid>.tmp' suffix, so the "
        "temp name that actually failed is %d chars and its directory is %d. The "
        "workspace directory is too deep -- point the project/workspace at a shorter "
        "absolute path."
        % (len(str(path)), limit, len(attempted), parent_len),
        attempted,
    )
    # OSError takes winerror only positionally on Windows; expose the original so callers
    # inspecting it still see what the OS reported.
    original_winerror = getattr(error, "winerror", None)
    if original_winerror is not None:
        try:
            raised.winerror = original_winerror
        except (AttributeError, TypeError):
            pass
    raise raised from error


def _write_atomically(path, write_body) -> None:
    """Shared tmp+replace for the atomic writers, with diagnosable deep-path failures."""
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        if os.name == "nt" and _is_path_too_long(error, str(path.parent)):
            _raise_path_too_long(path, str(path.parent), error, _WINDOWS_MAX_PATH)
        raise
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        write_body(tmp)
    except OSError as error:
        # The target itself was fine; the temp suffix is what overflowed. Report the temp
        # name, since that is the path the OS actually rejected.
        if os.name == "nt" and _is_path_too_long(error, str(tmp)):
            _raise_path_too_long(path, str(tmp), error, _WINDOWS_MAX_PATH)
        raise
    _replace_file(tmp, path)


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
    lock_key = str(lock_path.resolve())
    with _LOCAL_LOCKS_GUARD:
        local_lock = _LOCAL_LOCKS.setdefault(lock_key, threading.Lock())
    with local_lock:
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
    def body(tmp):
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")

    _write_atomically(path, body)


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
    def body(tmp):
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            for line in lines:
                f.write(line if line.endswith("\n") else line + "\n")

    _write_atomically(path, body)


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
    def body(tmp):
        with open(tmp, "w", encoding="utf-8", newline="") as f:
            f.write(text)

    _write_atomically(path, body)


def append_jsonl_event(path, event):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    row = dict(event)
    row.setdefault("schema_version", SCHEMA_VERSION)
    row.setdefault("ts", now_iso())
    with open(path, "a", encoding="utf-8", newline="\n") as f:
        f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")


def parent_inbox_path_for_task_dir(task_dir, bus_dir=None):
    """The parent's inbox for a child task dir.

    ``dirname(task_dir)/subagents/inbox.jsonl`` is the same wrong guess that put the
    child's event mirror in an orphan bus: on the product layout the task dir is
    ``<bus>/tasks/<name>``, so the guess resolves to ``<bus>/tasks/subagents`` while the
    parent reads ``<bus>``.

    Resolution order:
      1. ``bus_dir`` -- what the manager passes. The manager is the *parent* process, so
         its own environment carries no child hand-down; it knows the directory directly.
      2. ``GA_SUBAGENTS_STATE_DIR`` -- what a spawned child gets from its spawner.
      3. the historical guess, for a caller with neither (unit tests, legacy callers).
    """
    if bus_dir:
        return Path(bus_dir) / "inbox.jsonl"
    explicit = str(os.environ.get("GA_SUBAGENTS_STATE_DIR") or "").strip()
    if explicit:
        return Path(explicit) / "inbox.jsonl"
    return Path(task_dir).parent / "subagents" / "inbox.jsonl"


def append_parent_inbox_event(task_dir, event, bus_dir=None):
    row = dict(event)
    task_name = row.get("task_name") or Path(task_dir).name
    row.setdefault("type", "subagent_update")
    row.setdefault("author", f"/root/{task_name}")
    row.setdefault("recipient", "/root")
    row.setdefault("task_name", task_name)
    row.setdefault("agent_path", f"/root/{task_name}")
    row.setdefault("task_dir", str(task_dir))
    append_jsonl_event(parent_inbox_path_for_task_dir(task_dir, bus_dir=bus_dir), row)


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()
