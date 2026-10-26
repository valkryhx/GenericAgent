from __future__ import annotations

import copy
import itertools
import json
import os
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from genericagent.core import listing_cache
from genericagent import paths


PROJECT_ROOT = paths.PROJECT_ROOT
DEFAULT_ROOT = paths.SESSIONS_DIR

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
    anchor_uuid: str = ""
    first_uuid: str = ""
    last_uuid: str = ""


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
    last_message_uuid: str = ""
    events: list = field(default_factory=list)
    resumed_from_v1: bool = False
    branch_rounds: int = 0
    working_state: dict | None = None
    working_states: dict = field(default_factory=dict)


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
    with _transcript_write_lock:
        with p.open("a", encoding="utf-8", errors="replace") as fh:
            fh.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")


def transcript_schema_version():
    """Return the writer schema for new sessions (``2`` by default).

    ``GA_TRANSCRIPT_SCHEMA=v1`` is the emergency writer rollback switch. It
    affects only newly created sessions; existing v1 files are always read by
    the legacy parser and are never rewritten in place.
    """
    value = str(os.environ.get("GA_TRANSCRIPT_SCHEMA", "v2") or "v2").strip().lower()
    return 1 if value in {"1", "v1", "legacy"} else 2


def create_session(root=None, cwd=None, session_id=None, frontend=None, schema=None):
    sid = session_id or new_session_id()
    path = session_path(sid, root=root)
    schema = int(schema or transcript_schema_version())
    event = {
        "version": schema,
        "type": "session_start",
        "session_id": sid,
        "created_at": _now_iso(),
        "cwd": cwd or os.getcwd(),
        "frontend": frontend,
    }
    if schema == 2:
        event["schema"] = 2
    append_event(path, event)
    return str(path)


def record_turn(path, *, session_id, turn_id, source, user_text, assistant_text,
                backend_history_before, backend_history_after):
    if _detect_session_version(path) == 2:
        raise ValueError("record_turn is v1-only; use record_agent_turn/append_message for v2")
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
    version = _detect_session_version(path)
    append_event(path, {
        "version": version,
        "type": "compact",
        "session_id": session_id,
        "created_at": _now_iso(),
        "message": message or "",
        "backend_history_after": copy.deepcopy(backend_history_after or []),
    })


def record_rewind(path, *, session_id, keep_turns, backend_history_after):
    version = _detect_session_version(path)
    append_event(path, {
        "version": version,
        "type": "rewind",
        "session_id": session_id,
        "created_at": _now_iso(),
        "keep_turns": max(0, int(keep_turns or 0)),
        "backend_history_after": copy.deepcopy(backend_history_after or []),
    })


def _message_text_from_blocks(blocks):
    parts = []
    for block in blocks or []:
        if isinstance(block, dict) and block.get("type") == "text":
            value = block.get("text")
            if isinstance(value, str) and value.strip():
                parts.append(value)
    return chr(10).join(parts).strip()


def _message_from_v2_event(event):
    role = str(event.get("role") or "user")
    blocks = event.get("blocks")
    if not isinstance(blocks, list):
        text = str(event.get("text") or "")
        blocks = [{"type": "text", "text": text}] if text else []
    return {"role": role, "content": copy.deepcopy(blocks)}


def append_message(path, *, session_id, role, blocks, origin=None, parent_uuid=None,
                   turn_id=None, text="", uuid_value=None):
    event_uuid = str(uuid_value or uuid.uuid4().hex)
    event = {
        "version": 2,
        "type": "message",
        "session_id": session_id,
        "uuid": event_uuid,
        "parent_uuid": parent_uuid,
        "turn_id": None if turn_id is None else int(turn_id),
        "role": str(role or "user"),
        "blocks": copy.deepcopy(blocks or []),
        "created_at": _now_iso(),
    }
    if origin is not None:
        event["origin"] = copy.deepcopy(origin)
    if text:
        event["text"] = str(text)
    append_event(path, event)
    return event_uuid


_transcript_write_lock = threading.RLock()

_WORKING_STATE_KEYS = (
    "key_info",
    "related_sop",
    "passed_sessions",
    "active_skill",
    "active_skill_allowed_tools",
    "in_plan_mode",
)


def _lightweight_handler_working(working):
    if not isinstance(working, dict):
        return {}
    out = {}
    for key in _WORKING_STATE_KEYS:
        if key in working:
            value = working.get(key)
            if isinstance(value, (str, int, float, bool)) or value is None:
                out[key] = copy.deepcopy(value)
    records = working.get("_loaded_skill_records")
    if isinstance(records, dict):
        cleaned = {}
        for name, record in records.items():
            if isinstance(record, dict):
                cleaned[str(name)] = {
                    key: copy.deepcopy(record[key])
                    for key in ("name", "description", "source", "path", "base_dir", "content_chars", "state")
                    if key in record
                }
        if cleaned:
            out["_loaded_skill_records"] = cleaned
    hashes = working.get("_loaded_skill_hashes")
    if isinstance(hashes, dict):
        out["_loaded_skill_hashes"] = {str(key): str(value) for key, value in hashes.items()}
    return out


def append_working_state(
    path,
    *,
    session_id,
    handler_working,
    last_tools="",
    last_usage_tokens=None,
    turn_id=None,
):
    event = {
        "version": 2,
        "type": "working_state",
        "session_id": session_id,
        "created_at": _now_iso(),
        "handler_working": _lightweight_handler_working(handler_working),
        "last_tools": str(last_tools or ""),
        "last_usage_tokens": copy.deepcopy(last_usage_tokens),
    }
    if turn_id is not None:
        event["turn_id"] = int(turn_id)
    append_event(path, event)
    return event


def append_turn_end(path, *, session_id, turn_id, first_uuid, last_uuid):
    append_event(path, {
        "version": 2,
        "type": "turn_end",
        "session_id": session_id,
        "turn_id": int(turn_id),
        "first_uuid": first_uuid,
        "last_uuid": last_uuid,
        "created_at": _now_iso(),
    })


def _event_uuid(prefix):
    return f"{prefix}-{uuid.uuid4().hex}"


def _last_message_uuid(path):
    for event in reversed(_transcript_events(path)):
        if event.get("version") == 2 and event.get("type") == "message":
            return event.get("uuid")
    return None


def append_compact_boundary(
    path,
    *,
    session_id,
    trigger,
    pre_tokens=0,
    post_tokens=0,
    duration_ms=0,
    messages_summarized=0,
    logical_parent_uuid=None,
    preserved_uuids=None,
    preserved_segment=None,
    summary_uuid=None,
):
    """Append a compact boundary without copying the pre-compact history."""
    boundary_uuid = _event_uuid("b")
    preserved = [str(value) for value in (preserved_uuids or []) if str(value)]
    event = {
        "version": 2,
        "type": "compact_boundary",
        "session_id": session_id,
        "uuid": boundary_uuid,
        "parent_uuid": None,
        "logical_parent_uuid": logical_parent_uuid,
        "trigger": str(trigger or "manual"),
        "pre_tokens": int(pre_tokens or 0),
        "post_tokens": int(post_tokens or 0),
        "duration_ms": int(duration_ms or 0),
        "messages_summarized": int(messages_summarized or 0),
        "summary_uuid": summary_uuid,
        "preserved_uuids": preserved,
        "created_at": _now_iso(),
    }
    if preserved_segment:
        event["preserved_segment"] = copy.deepcopy(preserved_segment)
    append_event(path, event)
    return boundary_uuid


def append_compact_summary(
    path,
    *,
    session_id,
    text,
    parent_uuid,
    turn_id=None,
    uuid_value=None,
):
    """Append the transcript-only summary owned by a compact boundary."""
    summary_uuid = str(uuid_value or _event_uuid("s"))
    append_event(path, {
        "version": 2,
        "type": "summary",
        "session_id": session_id,
        "uuid": summary_uuid,
        "parent_uuid": parent_uuid,
        "turn_id": None if turn_id is None else int(turn_id),
        "text": str(text or ""),
        "is_compact_summary": True,
        "visible_in_transcript_only": True,
        "created_at": _now_iso(),
    })
    return summary_uuid


def record_compact_v2(
    path,
    *,
    session_id,
    summary,
    trigger="manual",
    pre_tokens=0,
    post_tokens=0,
    duration_ms=0,
    messages_summarized=0,
    preserved_uuids=None,
    preserved_segment=None,
    turn_id=None,
    keep_recent_turns=1,
    working_state=None,
):
    """Persist the CC-style boundary + summary pair for a v2 transcript."""
    if preserved_uuids is None:
        preserved_uuids, default_segment = compact_preserved_uuids(
            path, keep_recent_turns=keep_recent_turns
        )
        if preserved_segment is None:
            preserved_segment = default_segment
    summary_uuid = _event_uuid("s")
    boundary_uuid = append_compact_boundary(
        path,
        session_id=session_id,
        trigger=trigger,
        pre_tokens=pre_tokens,
        post_tokens=post_tokens,
        duration_ms=duration_ms,
        messages_summarized=messages_summarized,
        logical_parent_uuid=_last_message_uuid(path),
        preserved_uuids=preserved_uuids,
        preserved_segment=preserved_segment,
        summary_uuid=summary_uuid,
    )
    append_compact_summary(
        path,
        session_id=session_id,
        text=summary,
        parent_uuid=boundary_uuid,
        turn_id=turn_id,
        uuid_value=summary_uuid,
    )
    if isinstance(working_state, dict):
        append_working_state(
            path,
            session_id=session_id,
            handler_working=working_state.get("handler_working"),
            last_tools=working_state.get("last_tools") or "",
            last_usage_tokens=working_state.get("last_usage_tokens"),
            turn_id=turn_id,
        )
    return boundary_uuid, summary_uuid


def _truncate_events_at_turn(events, keep_turn_id):
    """Return events on the current branch through keep_turn_id."""
    keep = max(0, int(keep_turn_id or 0))
    current = []
    for event in events or []:
        if not isinstance(event, dict) or event.get("version") != 2:
            continue
        if event.get("type") == "rewind":
            current = [
                item for item in current
                if item.get("type") != "message"
                or item.get("turn_id") is None
                or int(item.get("turn_id")) <= keep
            ]
            continue
        current.append(event)
    return current


def load_history_at_anchor(path, *, anchor_uuid, keep_turn_id=None):
    """Rebuild request history through an explicit v2 message anchor."""
    from genericagent.core.projection import build_backend_history

    events = _read_branch_events(path)
    current = _truncate_events_at_turn(events, keep_turn_id if keep_turn_id is not None else 0)
    anchor = str(anchor_uuid or "")
    if not anchor:
        return None
    found = any(
        event.get("type") == "message" and str(event.get("uuid") or "") == anchor
        for event in current
    )
    if not found:
        return None
    cut = []
    for event in current:
        cut.append(event)
        if event.get("type") == "message" and str(event.get("uuid") or "") == anchor:
            break
    return build_backend_history(cut)


def compact_preserved_uuids(path, *, keep_recent_turns=1):
    """Return UUID anchors for the most recent completed turn(s).

    Compact must not copy message bodies into the boundary. This helper keeps
    the newest human-led turn and its tool/assistant messages by UUID so the
    projection can recover an exact tool-pairing chain without guessing from
    backend history.
    """
    keep = max(0, int(keep_recent_turns or 0))
    events = [event for event in _transcript_events(path) if event.get("version") == 2]
    if keep == 0:
        return [], {}
    turn_ids = []
    for event in events:
        if event.get("type") != "message":
            continue
        origin = event.get("origin") or {}
        role = str(event.get("role") or "")
        if role == "user" and not (isinstance(origin, dict) and origin.get("kind") == "tool_result"):
            turn_id = event.get("turn_id")
            if turn_id is not None:
                turn_ids.append(int(turn_id))
    unique_ids = []
    for turn_id in turn_ids:
        if turn_id not in unique_ids:
            unique_ids.append(turn_id)
    selected_turns = set(unique_ids[-keep:])
    selected = []
    for event in events:
        if event.get("type") != "message":
            continue
        turn_id = event.get("turn_id")
        if turn_id is not None and int(turn_id) in selected_turns:
            uuid_value = str(event.get("uuid") or "")
            if uuid_value:
                selected.append(uuid_value)
    segment = {}
    if selected:
        segment = {"head_uuid": selected[0], "tail_uuid": selected[-1]}
    return selected, segment


def record_rewind_v2(
    path,
    *,
    session_id,
    keep_turn_id,
    anchor_uuid=None,
    head_uuid=None,
    tail_uuid=None,
    working_state=None,
):
    """Append an anchor-based rewind marker for a v2 transcript."""
    keep = max(0, int(keep_turn_id or 0))
    if anchor_uuid is None:
        anchor_uuid = _turn_boundary_uuid(path, keep)
    event = {
        "version": 2,
        "type": "rewind",
        "session_id": session_id,
        "uuid": _event_uuid("r"),
        "anchor_uuid": anchor_uuid,
        "keep_turn_id": keep,
        "head_uuid": head_uuid,
        "tail_uuid": tail_uuid,
        "created_at": _now_iso(),
    }
    if working_state is not None:
        event["working_state"] = copy.deepcopy(working_state)
    append_event(path, event)
    return event["uuid"]


def _turn_boundary_uuid(path, keep_turn_id):
    events = _transcript_events(path)
    latest_boundary = None
    candidate = None
    for event in events:
        if event.get("version") != 2:
            continue
        if event.get("type") == "compact_boundary":
            latest_boundary = event
        if event.get("type") == "message":
            turn_id = event.get("turn_id")
            if turn_id is not None and int(turn_id) <= keep_turn_id:
                candidate = event.get("uuid") or candidate
    if candidate:
        return candidate
    return (latest_boundary or {}).get("uuid")


def _v2_delta_from_history(before, after, *, fallback_text=None):
    after = list(after or [])
    if isinstance(before, int):
        start = max(0, min(before, len(after)))
        return copy.deepcopy(after[start:])
    before = list(before or [])
    overlap = min(len(before), len(after))
    while overlap > 0 and before[-overlap:] != after[:overlap]:
        overlap -= 1
    if overlap:
        return copy.deepcopy(after[overlap:])
    if after and not before:
        return copy.deepcopy(after)
    if fallback_text:
        return [{"role": "user", "content": [{"type": "text", "text": str(fallback_text)}]}]
    return []


def record_workflow_event(path, *, session_id, run_id, event_type, artifact_dir, result_ref=None, error=None):
    event = {
        "version": _detect_session_version(path),
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


def load_v1_readonly(path):
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
            warnings.append(f"line {lineno}: unsupported v1 event")
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
        resumed_from_v1=True,
        branch_rounds=len(turns),
    )


def _v2_read_events(path):
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(str(path))
    events, warnings = [], []
    session_id, last_seq = "", 0
    for lineno, line in _iter_transcript_events(p):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except Exception as exc:
            warnings.append(f"line {lineno}: {exc}")
            continue
        if not isinstance(event, dict):
            warnings.append(f"line {lineno}: non-object event")
            continue
        session_id = event.get("session_id") or session_id
        seq = event.get("seq")
        if isinstance(seq, int) and seq > last_seq:
            last_seq = seq
        events.append(event)
    return p, events, session_id, warnings, last_seq


def _detect_session_version(path):
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(str(path))
    with p.open("r", encoding="utf-8", errors="replace") as fh:
        for line in fh:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except Exception:
                continue
            if isinstance(event, dict):
                version = event.get("version")
                return int(version) if isinstance(version, int) else 1
    return 1


def _iter_transcript_events(path):
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(str(path))
    with p.open("r", encoding="utf-8", errors="replace") as fh:
        for lineno, line in enumerate(fh, 1):
            yield lineno, line


def _transcript_events(path):
    events = []
    for _, line in _iter_transcript_events(path):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _load_v1_source_state(branch_events):
    """Replay immutable v1 source events into turns and backend history."""
    turns = []
    backend_history = []
    for event in branch_events:
        if event.get("version") != 1:
            continue
        kind = event.get("type")
        if kind == "turn":
            turn = TranscriptTurn(
                turn_id=int(event.get("turn_id") or len(turns) + 1),
                user_text=str(event.get("user_text") or ""),
                assistant_text=str(event.get("assistant_text") or ""),
                backend_history_before=copy.deepcopy(event.get("backend_history_before") or []),
                backend_history_after=copy.deepcopy(event.get("backend_history_after") or []),
            )
            keep = None
            if backend_history:
                keep = _find_turn_count_for_backend_history(turns, turn.backend_history_before)
            if keep is not None and keep < len(turns):
                del turns[keep:]
            turns.append(turn)
            backend_history = copy.deepcopy(turn.backend_history_after)
        elif kind == "compact":
            turns = []
            backend_history = copy.deepcopy(event.get("backend_history_after") or [])
        elif kind == "rewind":
            keep = max(0, min(int(event.get("keep_turns") or 0), len(turns)))
            del turns[keep:]
            backend_history = copy.deepcopy(event.get("backend_history_after") or [])
    return turns, backend_history


def _turns_to_ui_messages(turns):
    messages = []
    for turn in turns:
        if turn.user_text:
            messages.append({"role": "user", "content": turn.user_text})
        if turn.assistant_text:
            messages.append({"role": "assistant", "content": turn.assistant_text})
    return messages


def _read_branch_events(path, *, _seen=None):
    """Return source history followed by this branch's append-only events.

    A v1 source contributes all v1 events. A v2 source contributes its own
    history, then the branch marker selects its target. ``resumed_from`` is a
    topology marker, not a projected message, so it is omitted from the result.
    """
    p = Path(path).resolve()
    seen = set() if _seen is None else _seen
    if p in seen:
        return []
    seen.add(p)
    events = _transcript_events(p)
    if _detect_session_version(p) == 1:
        return events

    result = []
    for event in events:
        if event.get("type") == "resumed_from":
            source = str(event.get("source_path") or "").strip()
            if not source:
                continue
            source_path = Path(source)
            if not source_path.is_absolute():
                source_path = p.parent / source_path
            if not source_path.exists():
                continue
            result.extend(_read_branch_events(source_path, _seen=seen))
            continue
        result.append(event)
    return result


def _message_origin(role, blocks):
    if role == "assistant":
        return {"kind": "assistant"}
    if any(
        isinstance(block, dict) and block.get("type") == "tool_result"
        for block in blocks or []
    ):
        return {"kind": "tool_result"}
    return {"kind": "human"}


def load_v2(path):
    p, events, session_id, warnings, last_seq = _v2_read_events(path)
    branch_events = _read_branch_events(path)
    seen = set()
    turns = {}
    turn_order = []
    messages = []
    accepted_messages = []
    excluded_turns = set()
    summary = ""
    summary_uuid = ""
    working_state = None
    working_states = {}
    last_message_uuid = ""
    compact_history = None
    rewind_history = None

    for event in events:
        if event.get("version") != 2:
            warnings.append(f"v2 transcript contains non-v2 event: {event.get('type')}")
            continue
        kind = event.get("type")
        if kind == "message":
            event_uuid = str(event.get("uuid") or "")
            if not event_uuid:
                warnings.append("message without uuid")
                continue
            if event_uuid in seen:
                continue
            seen.add(event_uuid)
            turn_id = event.get("turn_id")
            if turn_id is not None and int(turn_id) in excluded_turns:
                continue
            # uuid first-wins applies to both the API projection and the UI/turn
            # reconstruction.  Keep the accepted event in a side list so a
            # duplicate cannot overwrite display text later.
            accepted_messages.append(event)
            messages.append(_message_from_v2_event(event))
            last_message_uuid = event_uuid
        elif kind == "turn_end":
            turn_id = int(event.get("turn_id") or len(turn_order) + 1)
            if turn_id not in turns:
                turns[turn_id] = {
                    "turn_id": turn_id,
                    "first_uuid": event.get("first_uuid"),
                    "last_uuid": event.get("last_uuid"),
                }
                turn_order.append(turn_id)
            else:
                turns[turn_id]["first_uuid"] = event.get("first_uuid") or turns[turn_id]["first_uuid"]
                turns[turn_id]["last_uuid"] = event.get("last_uuid") or turns[turn_id]["last_uuid"]
        elif kind == "summary":
            summary = str(event.get("text") or "")
            summary_uuid = str(event.get("uuid") or "")
        elif kind == "compact_boundary":
            # The boundary is a physical root. Its summary and preserved UUIDs
            # are projected from the canonical event stream; never from a
            # copied backend_history snapshot.
            excluded_turns.clear()
            turn_order = []
            turns.clear()
            messages = []
            compact_history = None
            rewind_history = None
        elif kind == "compact":
            # Legacy v2 compatibility: older writers stored a full post-compact
            # history. New writers use compact_boundary + summary.
            compact_history = copy.deepcopy(event.get("backend_history_after") or [])
            rewind_history = None
            excluded_turns.clear()
            turn_order = []
            turns.clear()
            messages = []
        elif kind == "rewind":
            keep = int(event.get("keep_turn_id") or event.get("keep_turns") or 0)
            excluded_turns.update(tid for tid in list(turns) if tid > keep)
            turn_order = [tid for tid in turn_order if tid <= keep]
            rewind_history = copy.deepcopy(event.get("backend_history_after") or []) or None
            compact_history = None
            rewind_state = event.get("working_state")
            if isinstance(rewind_state, dict):
                working_state = copy.deepcopy(rewind_state)
        elif kind == "working_state":
            working_state = copy.deepcopy(event)
            state_turn = event.get("turn_id")
            if state_turn is not None:
                working_states[int(state_turn)] = copy.deepcopy(event)
        elif kind in {"session_start", "resumed_from", "workflow_started", "workflow_completed", "workflow_failed"}:
            continue
        else:
            warnings.append(f"unsupported v2 event: {kind}")

    turn_text = {}
    for event in accepted_messages:
        turn_id = event.get("turn_id")
        if turn_id is None or int(turn_id) in excluded_turns:
            continue
        role = str(event.get("role") or "")
        origin = event.get("origin") or {}
        if role == "user" and not (isinstance(origin, dict) and origin.get("kind") == "tool_result"):
            value = str(event.get("text") or _message_text_from_blocks(event.get("blocks")))
            if value:
                turn_text.setdefault(int(turn_id), {})["user_text"] = value
        elif role == "assistant":
            value = str(event.get("text") or _message_text_from_blocks(event.get("blocks")))
            if value:
                turn_text.setdefault(int(turn_id), {})["assistant_text"] = value

    loaded_turns, ui_messages = [], []
    for turn_id in turn_order:
        raw = turns[turn_id]
        text = turn_text.get(turn_id) or {}
        item = TranscriptTurn(
            turn_id=turn_id,
            user_text=str(text.get("user_text") or ""),
            assistant_text=str(text.get("assistant_text") or ""),
            backend_history_before=[],
            backend_history_after=[],
            anchor_uuid=str(raw.get('last_uuid') or ''),
            first_uuid=str(raw.get('first_uuid') or ''),
            last_uuid=str(raw.get('last_uuid') or ''),
        )
        loaded_turns.append(item)
        if item.user_text:
            ui_messages.append({"role": "user", "content": item.user_text})
        if item.assistant_text:
            ui_messages.append({"role": "assistant", "content": item.assistant_text})

    if rewind_history is not None:
        messages = copy.deepcopy(rewind_history)
    elif compact_history is not None:
        messages = copy.deepcopy(compact_history)
    else:
        from genericagent.core.projection import build_backend_history
        source_turns, source_history = _load_v1_source_state(branch_events)
        messages = build_backend_history(
            [event for event in branch_events if event.get("version") == 2]
        )
        if source_history:
            messages = copy.deepcopy(source_history) + copy.deepcopy(messages)

    source_turns, _ = _load_v1_source_state(branch_events)
    local_rounds = len(loaded_turns)
    if source_turns:
        merged = []
        for index, turn in enumerate(source_turns + loaded_turns, 1):
            merged.append(TranscriptTurn(
                turn_id=index,
                user_text=turn.user_text,
                assistant_text=turn.assistant_text,
                backend_history_before=turn.backend_history_before,
                backend_history_after=turn.backend_history_after,
                anchor_uuid=str(getattr(turn, "anchor_uuid", "") or ""),
                first_uuid=str(getattr(turn, "first_uuid", "") or ""),
                last_uuid=str(getattr(turn, "last_uuid", "") or ""),
            ))
        loaded_turns = merged
        ui_messages = _turns_to_ui_messages(loaded_turns)
    preview = next((turn.user_text.strip() for turn in loaded_turns if turn.user_text.strip()), "")
    stat = p.stat()
    return LoadedSession(
        path=str(p),
        session_id=session_id or p.stem,
        mtime=stat.st_mtime,
        preview=preview,
        rounds=len(loaded_turns),
        ui_messages=ui_messages,
        backend_history=copy.deepcopy(messages),
        turns=loaded_turns,
        warnings=warnings,
        last_seq=last_seq,
        last_message_uuid=last_message_uuid,
        events=copy.deepcopy(events),
        branch_rounds=local_rounds,
        working_state=copy.deepcopy(working_state),
        working_states=copy.deepcopy(working_states),
    )


def load_session(path):
    if _detect_session_version(path) == 2:
        return load_v2(path)
    return load_v1_readonly(path)


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
    version = _detect_session_version(p)
    branch_events = _read_branch_events(p) if version == 2 else _transcript_events(p)
    if version == 2:
        source_turns, _ = _load_v1_source_state(branch_events)
        turns.extend((turn.user_text, 0) for turn in source_turns)
    for event in branch_events:
        if not isinstance(event, dict) or event.get("version") != version:
            continue
        if version == 2:
            kind = event.get("type")
            session_id = event.get("session_id") or session_id
            seq = event.get("seq")
            if isinstance(seq, int) and seq > last_seq:
                last_seq = seq
            if kind == "message":
                turn_id = event.get("turn_id")
                role = event.get("role")
                origin = event.get("origin") or {}
                if turn_id is not None and role == "user" and not (
                    isinstance(origin, dict) and origin.get("kind") == "tool_result"
                ):
                    text_value = str(event.get("text") or _message_text_from_blocks(event.get("blocks")))
                    if text_value:
                        turns.append((text_value, turn_id))
            elif kind == "compact_boundary":
                turns = []
                backend_after = None
            elif kind == "compact":
                turns = []
                backend_after = event.get("backend_history_after") or []
            elif kind == "rewind":
                keep = int(event.get("keep_turn_id") or 0)
                turns = [
                    item for item in turns
                    if item[1] == 0 or int(item[1]) <= keep
                ]
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
    state = loaded.working_state or {}
    if client is not None:
        if hasattr(client, "last_tools") and state.get("last_tools") is not None:
            client.last_tools = str(state.get("last_tools") or "")
        usage = state.get("last_usage_tokens")
        backend_for_state = getattr(client, "backend", None)
        if isinstance(usage, dict) and backend_for_state is not None:
            backend_for_state.last_usage_tokens = copy.deepcopy(usage)
    if hasattr(agent, "handler"):
        agent.handler = None
    agent._resumed_working_state = copy.deepcopy(state) if isinstance(state, dict) else None
    agent.session_id = loaded.session_id
    agent.session_path = loaded.path
    agent.session_turn_id = int(loaded.branch_rounds or loaded.rounds)
    agent._resumed_from_v1 = bool(loaded.resumed_from_v1)
    return RestoreResult(
        ok=True,
        message=f"已恢复 {loaded.rounds} 轮结构化会话（{Path(path).name}）",
        session=loaded,
    )


def ensure_agent_session(agent, *, root=None, frontend=None):
    resumed_from_v1 = bool(getattr(agent, "_resumed_from_v1", False))
    resumed_from_v1_path = getattr(agent, "session_path", None)
    if root is None and resumed_from_v1_path:
        root = str(Path(resumed_from_v1_path).parent)
    if getattr(agent, "session_path", None) and getattr(agent, "session_id", None):
        if resumed_from_v1 and _detect_session_version(agent.session_path) == 1:
            # A v1 file is read-only. Continuing a restored v1 session creates a
            # new v2 branch file instead of appending v2 events to the legacy
            # transcript. The old file remains available for audit/rollback.
            agent.session_path = None
            agent.session_id = None
            agent.session_turn_id = 0
        else:
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
    agent._resumed_from_v1 = False
    if resumed_from_v1:
        agent._resumed_from_v1_path = resumed_from_v1_path
        try:
            source_rounds = load_session(resumed_from_v1_path).rounds
        except Exception:
            source_rounds = 0
        append_event(path, {
            "version": 2,
            "type": "resumed_from",
            "session_id": sid,
            "source_path": resumed_from_v1_path,
            "source_rounds": int(source_rounds or 0),
            "created_at": _now_iso(),
        })
    return path


def current_backend_history(agent):
    backend = getattr(getattr(agent, "llmclient", None), "backend", None)
    return copy.deepcopy(getattr(backend, "history", []) or [])


def _backend_history_tail(agent, start):
    backend = getattr(getattr(agent, "llmclient", None), "backend", None)
    history = getattr(backend, "primary", backend)
    history = getattr(history, "history", []) if history is not None else []
    start = max(0, min(int(start or 0), len(history or [])))
    return copy.deepcopy((history or [])[start:])


def _record_agent_turn_v2(agent, path, *, turn_id, user_text, assistant_text,
                          backend_history_before):
    session_id = getattr(agent, "session_id")
    if isinstance(backend_history_before, int):
        delta = _backend_history_tail(agent, backend_history_before)
    else:
        before = list(backend_history_before or [])
        after = current_backend_history(agent)
        delta = _v2_delta_from_history(
            before, after, fallback_text=user_text or assistant_text or ""
        )

    # The backend history after a real turn normally contains the user request
    # and one or more assistant/tool-result messages.  Persist only that delta.
    first_uuid = None
    last_uuid = None
    parent_uuid = None
    for message in delta:
        role = str(message.get("role") or "user")
        blocks = message.get("content")
        if isinstance(blocks, str):
            blocks = [{"type": "text", "text": blocks}]
        elif not isinstance(blocks, list):
            blocks = [{"type": "text", "text": str(blocks)}]
        origin = _message_origin(role, blocks)
        event_uuid = append_message(
            path,
            session_id=session_id,
            role=role,
            blocks=blocks,
            origin=origin,
            parent_uuid=parent_uuid,
            turn_id=turn_id,
            text=_message_text_from_blocks(blocks),
        )
        first_uuid = first_uuid or event_uuid
        last_uuid = event_uuid
        parent_uuid = event_uuid

    if first_uuid is None:
        text = str(user_text or "")
        if text:
            first_uuid = append_message(
                path,
                session_id=session_id,
                role="user",
                blocks=[{"type": "text", "text": text}],
                origin={"kind": "human"},
                turn_id=turn_id,
                text=text,
            )
            last_uuid = first_uuid
    if last_uuid is None:
        text = str(assistant_text or "")
        if text:
            last_uuid = append_message(
                path,
                session_id=session_id,
                role="assistant",
                blocks=[{"type": "text", "text": text}],
                origin={"kind": "assistant"},
                parent_uuid=first_uuid,
                turn_id=turn_id,
                text=text,
            )
            first_uuid = first_uuid or last_uuid
    if first_uuid is None:
        return
    append_turn_end(
        path,
        session_id=session_id,
        turn_id=turn_id,
        first_uuid=first_uuid,
        last_uuid=last_uuid,
    )
    handler = getattr(agent, "handler", None)
    working = getattr(handler, "working", None) if handler is not None else None
    client = getattr(agent, "llmclient", None)
    backend = getattr(client, "backend", None)
    append_working_state(
        path,
        session_id=session_id,
        handler_working=working,
        last_tools=getattr(client, "last_tools", "") if client is not None else "",
        last_usage_tokens=getattr(backend, "last_usage_tokens", None) if backend is not None else None,
        turn_id=turn_id,
    )


def record_agent_turn(agent, *, user_text, assistant_text, source, backend_history_before):
    path = ensure_agent_session(agent)
    turn_id = int(getattr(agent, "session_turn_id", 0) or 0) + 1
    agent.session_turn_id = turn_id
    if _detect_session_version(path) == 2:
        _record_agent_turn_v2(
            agent,
            path,
            turn_id=turn_id,
            user_text=user_text,
            assistant_text=assistant_text,
            backend_history_before=backend_history_before,
        )
        return
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
