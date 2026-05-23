from __future__ import annotations

import copy
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_ROOT = PROJECT_ROOT / "temp" / "sessions"


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
    )


def list_sessions(root=None, exclude_session_id=None, include_empty=False):
    out = []
    base = _root(root)
    if not base.exists():
        return []
    for p in base.glob("session_*.jsonl"):
        try:
            loaded = load_session(p)
        except Exception:
            continue
        if exclude_session_id and loaded.session_id == exclude_session_id:
            continue
        if not include_empty and loaded.rounds <= 0:
            continue
        out.append(loaded)
    out.sort(key=lambda item: item.mtime, reverse=True)
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
