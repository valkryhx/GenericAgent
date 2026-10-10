from __future__ import annotations

import copy
import itertools
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import listing_cache


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_ROOT = PROJECT_ROOT / "temp" / "sessions"

# Process-monotonic write counter. Persisted per event as ``seq`` so
# ``list_sessions`` can break filesystem-mtime ties deterministically: on
# platforms with coarse timestamp resolution (e.g. Windows ~15ms) several
# rapid appends can share the same mtime, and glob order is not creation
# order. The seq reflects true write order within a process run.
_event_seq = itertools.count(1)


@dataclass
class TranscriptTurn:
    turn_id: int
    user_text: str
    assistant_text: str
    backend_history_before: list
    backend_history_after: list


@dataclass
class LoadedSession:
    path: str
    session_id: str
    mtime: float
    preview: str
    rounds: int
    ui_messages: list
    backend_history: list
    turns: list[TranscriptTurn] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    last_seq: int = 0


@dataclass
class SessionSummary:
    """Lightweight ``/resume`` listing row -- not a restorable session.

    Listing runs on every picker open (and twice per resume), so it must not pay
    for a full restore: ``load_session()`` parses every event and deep-copies the
    backend history of every turn, while the picker only needs identity, preview,
    round count and the user texts (``continue_cmd`` uses those to hide a legacy
    log a transcript already covers).  There is deliberately no ``ui_messages`` /
    ``backend_history`` here -- call ``load_session()`` when the data is needed.
    """

    path: str
    session_id: str
    mtime: float
    preview: str
    rounds: int
    last_seq: int = 0
    user_texts: list = field(default_factory=list)


@dataclass
class RestoreResult:
    ok: bool
    message: str
    session: LoadedSession | None = None


def _now_iso():
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _root(root=None):
    return Path(root) if root is not None else DEFAULT_ROOT


def new_session_id():
    return "session_" + uuid.uuid4().hex


def session_path(session_id, root=None):
    return _root(root) / f"{session_id}.jsonl"


def is_transcript_path(path):
    return str(path or "").lower().endswith(".jsonl")


def append_event(path, event):
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    event = dict(event)
    event.setdefault("seq", next(_event_seq))
    with p.open("a", encoding="utf-8", errors="replace") as fh:
        fh.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")


def create_session(root=None, cwd=None, session_id=None, frontend=None):
    sid = session_id or new_session_id()
    path = session_path(sid, root=root)
    append_event(path, {
        "version": 1,
        "type": "session_start",
        "session_id": sid,
        "created_at": _now_iso(),
        "cwd": cwd or os.getcwd(),
        "frontend": frontend,
    })
    return str(path)


def record_turn(path, *, session_id, turn_id, source, user_text, assistant_text,
                backend_history_before, backend_history_after):
    append_event(path, {
        "version": 1,
        "type": "turn",
        "session_id": session_id,
        "turn_id": int(turn_id),
        "created_at": _now_iso(),
        "source": source,
        "user_text": user_text or "",
        "assistant_text": assistant_text or "",
        "backend_history_before": copy.deepcopy(backend_history_before or []),
        "backend_history_after": copy.deepcopy(backend_history_after or []),
    })


def record_compact(path, *, session_id, message, backend_history_after):
    append_event(path, {
        "version": 1,
        "type": "compact",
        "session_id": session_id,
        "created_at": _now_iso(),
        "message": message or "",
        "backend_history_after": copy.deepcopy(backend_history_after or []),
    })


def record_rewind(path, *, session_id, keep_turns, backend_history_after):
    append_event(path, {
        "version": 1,
        "type": "rewind",
        "session_id": session_id,
        "created_at": _now_iso(),
        "keep_turns": max(0, int(keep_turns or 0)),
        "backend_history_after": copy.deepcopy(backend_history_after or []),
    })


def record_workflow_event(path, *, session_id, run_id, event_type, artifact_dir, result_ref=None, error=None):
    event = {
        "version": 1,
        "type": event_type,
        "session_id": session_id,
        "created_at": _now_iso(),
        "run_id": run_id,
        "artifact_dir": artifact_dir,
    }
    if result_ref is not None:
        event["result_ref"] = result_ref
    if error is not None:
        event["error"] = error
    append_event(path, event)


def _history_equal(left, right):
    return (left or []) == (right or [])


def _find_turn_count_for_backend_history(turns, backend_history):
    if not backend_history:
        return 0
    for idx in range(len(turns) - 1, -1, -1):
        if _history_equal(turns[idx].backend_history_after, backend_history):
            return idx + 1
    return None


def _append_loaded_turn(turns, ui_messages, turn):
    turns.append(turn)
    if turn.user_text.strip():
        ui_messages.append({"role": "user", "content": turn.user_text})
    if turn.assistant_text.strip():
        ui_messages.append({"role": "assistant", "content": turn.assistant_text})


def _truncate_loaded_turns(turns, ui_messages, keep_turns):
    keep = max(0, min(int(keep_turns or 0), len(turns)))
    del turns[keep:]
    keep_messages = 0
    for turn in turns:
        if turn.user_text.strip():
            keep_messages += 1
        if turn.assistant_text.strip():
            keep_messages += 1
    del ui_messages[keep_messages:]


def load_session(path):
    p = Path(path)
    warnings = []
    session_id = ""
    turns = []
    ui_messages = []
    backend_history = []
    last_seq = 0
    if not p.exists():
        raise FileNotFoundError(str(path))
    for lineno, line in enumerate(p.read_text(encoding="utf-8", errors="replace").splitlines(), 1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except Exception as exc:
            warnings.append(f"line {lineno}: {exc}")
            continue
        if not isinstance(event, dict) or event.get("version") != 1:
            warnings.append(f"line {lineno}: unsupported event")
            continue
        session_id = event.get("session_id") or session_id
        seq = event.get("seq")
        if isinstance(seq, int) and seq > last_seq:
            last_seq = seq
        if event.get("type") == "turn":
            turn = TranscriptTurn(
                turn_id=int(event.get("turn_id") or len(turns) + 1),
                user_text=str(event.get("user_text") or ""),
                assistant_text=str(event.get("assistant_text") or ""),
                backend_history_before=copy.deepcopy(event.get("backend_history_before") or []),
                backend_history_after=copy.deepcopy(event.get("backend_history_after") or []),
            )
            keep_turns = None
            if backend_history:
                keep_turns = _find_turn_count_for_backend_history(turns, turn.backend_history_before)
            if keep_turns is not None and keep_turns < len(turns):
                _truncate_loaded_turns(turns, ui_messages, keep_turns)
            _append_loaded_turn(turns, ui_messages, turn)
            backend_history = copy.deepcopy(turn.backend_history_after)
        elif event.get("type") == "compact":
            backend_history = copy.deepcopy(event.get("backend_history_after") or [])
        elif event.get("type") == "rewind":
            keep_turns = int(event.get("keep_turns") or 0)
            _truncate_loaded_turns(turns, ui_messages, keep_turns)
            backend_history = copy.deepcopy(event.get("backend_history_after") or [])
    preview = next((t.user_text.strip() for t in turns if t.user_text.strip()), "")
    stat = p.stat()
    return LoadedSession(
        path=str(p),
        session_id=session_id or p.stem,
        mtime=stat.st_mtime,
        preview=preview,
        rounds=len(turns),
        ui_messages=ui_messages,
        backend_history=backend_history,
        turns=turns,
        warnings=warnings,
        last_seq=last_seq,
    )


def _listing_cache_path(base):
    return str(base / ".listing_cache.json")


def _scan_session_summary(path, stat):
    """Scan one transcript for the listing fields only (no deep copies).

    Mirrors ``load_session``'s turn bookkeeping exactly -- inferred rewinds
    (``backend_history_before`` matching an earlier ``backend_history_after``),
    explicit ``rewind`` events and ``compact`` events truncate identically -- so
    ``preview``/``rounds``/``last_seq`` agree with a full load.
    """
    p = Path(path)
    session_id = ""
    last_seq = 0
    turns = []            # (user_text, backend_history_after)
    backend_after = None
    for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        if not isinstance(event, dict) or event.get("version") != 1:
            continue
        session_id = event.get("session_id") or session_id
        seq = event.get("seq")
        if isinstance(seq, int) and seq > last_seq:
            last_seq = seq
        kind = event.get("type")
        if kind == "turn":
            before = event.get("backend_history_before") or []
            after = event.get("backend_history_after") or []
            keep = None
            if backend_after:
                for idx in range(len(turns) - 1, -1, -1):
                    if turns[idx][1] == before:
                        keep = idx + 1
                        break
            if keep is not None and keep < len(turns):
                del turns[keep:]
            turns.append((str(event.get("user_text") or ""), after))
            backend_after = after
        elif kind == "compact":
            backend_after = event.get("backend_history_after") or []
        elif kind == "rewind":
            keep = max(0, min(int(event.get("keep_turns") or 0), len(turns)))
            del turns[keep:]
            backend_after = event.get("backend_history_after") or []
    preview = next((text.strip() for text, _ in turns if text.strip()), "")
    return {
        "mtime": stat.st_mtime,
        "size": stat.st_size,
        "session_id": session_id or p.stem,
        "preview": preview,
        "rounds": len(turns),
        "last_seq": last_seq,
        "user_texts": [text.strip() for text, _ in turns if text.strip()],
    }


def list_sessions(root=None, exclude_session_id=None, include_empty=False):
    """Summaries of ``session_*.jsonl`` under ``root``, newest-first.

    Each file is scanned once per (mtime, size) and the summary is cached next to
    it (``.listing_cache.json``); unchanged files are never re-parsed.
    """
    base = _root(root)
    if not base.exists():
        return []
    cached = listing_cache.load(_listing_cache_path(base))
    entries = {}
    changed = False
    out = []
    for p in base.glob("session_*.jsonl"):
        try:
            stat = p.stat()
        except OSError:
            continue
        key = str(p)
        entry = cached.get(key)
        if not listing_cache.is_fresh(entry, stat):
            try:
                entry = _scan_session_summary(p, stat)
            except Exception:
                continue
            changed = True
        entries[key] = entry
        if exclude_session_id and entry.get("session_id") == exclude_session_id:
            continue
        rounds = int(entry.get("rounds") or 0)
        if not include_empty and rounds <= 0:
            continue
        out.append(SessionSummary(
            path=key,
            session_id=entry.get("session_id") or Path(key).stem,
            mtime=stat.st_mtime,
            preview=entry.get("preview") or "",
            rounds=rounds,
            last_seq=int(entry.get("last_seq") or 0),
            user_texts=list(entry.get("user_texts") or []),
        ))
    # 只有真的重扫过、或缓存里有多余条目（文件被删/改名）时才落盘：整表深比较本身
    # 就要上百毫秒，而命中缓存时每个条目按定义都完全相同。
    if changed or len(entries) != len(cached):
        listing_cache.save(_listing_cache_path(base), entries)
    # Sort newest-first. mtime is the primary key so genuinely newer files win.
    # last_seq breaks mtime ties by true write order (coarse-resolution clocks
    # can collapse rapid appends to the same mtime), and session_id is a final
    # deterministic tiebreaker for legacy files that predate the seq field.
    out.sort(key=lambda item: (item.mtime, item.last_seq, item.session_id), reverse=True)
    return out


def restore_agent_session(agent, path):
    loaded = load_session(path)
    try:
        agent.abort()
    except Exception:
        pass
    backend = getattr(getattr(agent, "llmclient", None), "backend", None)
    if backend is not None and hasattr(backend, "history"):
        backend.history = copy.deepcopy(loaded.backend_history)
    if hasattr(agent, "history"):
        agent.history = []
    client = getattr(agent, "llmclient", None)
    if client is not None and hasattr(client, "last_tools"):
        client.last_tools = ""
    if hasattr(agent, "handler"):
        agent.handler = None
    agent.session_id = loaded.session_id
    agent.session_path = loaded.path
    agent.session_turn_id = loaded.rounds
    return RestoreResult(
        ok=True,
        message=f"已恢复 {loaded.rounds} 轮结构化会话（{Path(path).name}）",
        session=loaded,
    )


def ensure_agent_session(agent, *, root=None, frontend=None):
    if getattr(agent, "session_path", None) and getattr(agent, "session_id", None):
        if not hasattr(agent, "session_turn_id"):
            try:
                agent.session_turn_id = load_session(agent.session_path).rounds
            except Exception:
                agent.session_turn_id = 0
        return agent.session_path
    sid = new_session_id()
    path = create_session(root=root, cwd=os.getcwd(), session_id=sid, frontend=frontend)
    agent.session_id = sid
    agent.session_path = path
    agent.session_turn_id = 0
    return path


def current_backend_history(agent):
    backend = getattr(getattr(agent, "llmclient", None), "backend", None)
    return copy.deepcopy(getattr(backend, "history", []) or [])


def record_agent_turn(agent, *, user_text, assistant_text, source, backend_history_before):
    path = ensure_agent_session(agent)
    turn_id = int(getattr(agent, "session_turn_id", 0) or 0) + 1
    agent.session_turn_id = turn_id
    record_turn(
        path,
        session_id=getattr(agent, "session_id"),
        turn_id=turn_id,
        source=source,
        user_text=user_text,
        assistant_text=assistant_text,
        backend_history_before=backend_history_before,
        backend_history_after=current_backend_history(agent),
    )
