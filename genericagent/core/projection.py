"""Read-only projections from canonical transcript events.

Phase B separates the two consumers that v1 conflated:

* backend/API history is a request-safe *copy*;
* UI history is another copy, without the API window.

Neither function mutates the canonical event list or a live backend history.
Compact/rewind remain legacy snapshot inputs in this phase.
"""
from __future__ import annotations

import copy

MAX_PROJECTION_MESSAGES = 4096


def _content_blocks(content):
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [copy.deepcopy(block) for block in content if isinstance(block, dict)]
    if isinstance(content, dict):
        return [copy.deepcopy(content)]
    return [{"type": "text", "text": str(content)}]


def _message_from_event(event):
    return {
        "role": str(event.get("role") or "user"),
        "content": _content_blocks(event.get("blocks", event.get("content", ""))),
    }


def _uuid_key(event):
    return str(event.get("uuid") or "")


def _is_tool_result_message(message):
    content = message.get("content")
    return isinstance(content, list) and any(
        isinstance(block, dict) and block.get("type") == "tool_result"
        for block in content
    )


def _is_real_user(message):
    return message.get("role") == "user" and not _is_tool_result_message(message)


def _is_real_user_event(event):
    origin = event.get("origin") or {}
    return (
        event.get("role") == "user"
        and not (isinstance(origin, dict) and origin.get("kind") == "tool_result")
    )


def _pair_messages(messages):
    """Return request-safe copies with tool_use/tool_result repairs.

    Missing results are made explicit; orphan results are converted to text so
    a projection never starts with a dangling tool_result reference.
    """
    out = []
    pending_uses = set()
    for message in messages:
        item = copy.deepcopy(message)
        blocks = _content_blocks(item.get("content"))
        if item.get("role") == "assistant":
            uses = {
                str(block.get("id") or "")
                for block in blocks
                if block.get("type") == "tool_use" and block.get("id")
            }
            pending_uses = uses
        elif item.get("role") == "user":
            results = {
                str(block.get("tool_use_id") or "")
                for block in blocks
                if block.get("type") == "tool_result"
            }
            repaired = []
            for block in blocks:
                if block.get("type") == "tool_result" and block.get("tool_use_id") not in pending_uses:
                    repaired.append({"type": "text", "text": str(block.get("content") or "")})
                else:
                    repaired.append(block)
            missing = [
                {"type": "tool_result", "tool_use_id": uid, "content": "(missing tool result)"}
                for uid in pending_uses
                if uid and uid not in results
            ]
            if missing:
                repaired = missing + repaired
            item["content"] = repaired
            pending_uses = set()
        out.append(item)

    if pending_uses:
        out.append({
            "role": "user",
            "content": [
                {"type": "tool_result", "tool_use_id": uid, "content": "(missing tool result)"}
                for uid in pending_uses
                if uid
            ],
        })
    return out


def _trim_projection(messages, max_messages=MAX_PROJECTION_MESSAGES):
    if max_messages <= 0 or len(messages) <= max_messages:
        return messages
    start = max(0, len(messages) - int(max_messages))
    while start < len(messages) and not _is_real_user(messages[start]):
        start += 1
    return messages[start:] if start < len(messages) else messages[-1:]


def _event_uuid(event):
    return str(event.get("uuid") or "")


def _latest_boundary(events):
    latest = None
    for event in events or []:
        if event.get("type") == "compact_boundary":
            latest = event
    return latest


def _summary_by_uuid(events):
    return {
        str(event.get("uuid") or ""): event
        for event in events or []
        if event.get("type") == "summary" and event.get("uuid")
    }


def _compact_projection(events, *, max_messages=MAX_PROJECTION_MESSAGES):
    """Project the latest compact boundary without using a history snapshot.

    The request view is the compact summary, the explicitly preserved messages
    before the boundary, then every current-branch message after the boundary.
    Missing summary/source events return ``None`` so callers can safely fall
    back to the linear projection; a missing preserved UUID is skipped rather
    than reconstructed from unrelated content.
    """
    current = current_branch_events(events)
    boundary_index = None
    boundary = None
    for index, event in enumerate(current):
        if event.get("type") == "compact_boundary":
            boundary_index = index
            boundary = event
    if boundary is None or boundary_index is None:
        return None
    summary_uuid = str(boundary.get("summary_uuid") or "")
    summary = _summary_by_uuid(current).get(summary_uuid)
    if summary is None:
        return None
    by_uuid = {
        _event_uuid(event): event
        for event in current
        if event.get("type") == "message" and _event_uuid(event)
    }
    selected = []
    preserved_uuids = boundary.get("preserved_uuids") or []
    # An explicitly empty preserved_uuids list is common for a writer that
    # uses only preserved_segment. Prefer the segment whenever no UUID list is
    # present, rather than treating [] as "nothing to preserve".
    segment = boundary.get("preserved_segment") or {}
    if preserved_uuids:
        for uuid_value in preserved_uuids:
            event = by_uuid.get(str(uuid_value))
            if event is not None:
                selected.append(event)
    elif segment:
        head_uuid = str(segment.get("head_uuid") or "")
        tail_uuid = str(segment.get("tail_uuid") or "")
        if head_uuid and tail_uuid:
            chain = []
            current_uuid = tail_uuid
            seen = set()
            while current_uuid and current_uuid not in seen:
                seen.add(current_uuid)
                event = by_uuid.get(current_uuid)
                if event is None:
                    chain = []
                    break
                chain.append(event)
                if current_uuid == head_uuid:
                    break
                current_uuid = str(event.get("parent_uuid") or "")
            if chain and chain[-1].get("uuid") == head_uuid:
                selected = list(reversed(chain))
    messages = [{
        "role": "user",
        "content": [{
            "type": "text",
            "text": "<compact_summary>" + chr(10) + str(summary.get("text") or "") + chr(10) + "</compact_summary>",
        }],
    }]
    messages.extend(_message_from_event(event) for event in selected)
    selected_uuids = {_event_uuid(event) for event in selected}
    for event in current[boundary_index + 1:]:
        if event.get("type") != "message":
            continue
        uuid_value = _event_uuid(event)
        if uuid_value and uuid_value in selected_uuids:
            continue
        messages.append(_message_from_event(event))
    repaired = _pair_messages(messages)
    return copy.deepcopy(_trim_projection(repaired, max_messages=max_messages))


def current_branch_events(events):
    """Return v2 events on the latest valid rewind branch.

    A rewind removes turns strictly above its keep_turn_id.  A later turn may
    reuse that same id on the new branch; that is valid and must not be
    mistaken for the abandoned turn seen before the rewind.
    """
    current, since_rewind = [], False
    for event in events or []:
        if not isinstance(event, dict) or event.get("version") != 2:
            continue
        if event.get("type") == "rewind":
            keep = int(event.get("keep_turn_id") or event.get("keep_turns") or 0)
            current = [
                item for item in current
                if item.get("type") != "message"
                or item.get("turn_id") is None
                or int(item.get("turn_id")) <= keep
            ]
            current.append(event)
            since_rewind = True
            continue
        turn_id = event.get("turn_id")
        if since_rewind and event.get("type") == "message" and turn_id is not None:
            # Messages after a rewind belong to the new branch regardless of
            # whether their turn_id was reused from the abandoned branch.
            current.append(event)
            continue
        current.append(event)
    return current


def build_backend_history(events, *, max_messages=MAX_PROJECTION_MESSAGES):
    """Project canonical v2 message events into a request-safe copy.

    Only linear v2 history is handled here. Compact boundary projection is
    deliberately deferred to Phase C; callers with a legacy v1 snapshot should
    keep using that snapshot until Phase C lands.
    """
    compacted = _compact_projection(events, max_messages=max_messages)
    if compacted is not None:
        return compacted
    messages = []
    seen = set()
    for event in current_branch_events(events):
        if event.get("type") != "message":
            continue
        uuid_value = _uuid_key(event)
        if uuid_value:
            if uuid_value in seen:
                continue
            seen.add(uuid_value)
        messages.append(_message_from_event(event))
    repaired = _pair_messages(messages)
    return copy.deepcopy(_trim_projection(repaired, max_messages=max_messages))


def build_ui_messages(events):
    """Project canonical v2 events into UI replay rows.

    UI is intentionally not API-windowed. Tool results are omitted from the
    plain chat rows; the Ink bridge currently composes tool rows from its live
    event stream and legacy Assistant text.
    """
    current = current_branch_events(events)
    latest_boundary = None
    for index, event in enumerate(current):
        if event.get("type") == "compact_boundary":
            latest_boundary = index
    if latest_boundary is not None:
        # UI history before a compact boundary is owned by the summary. Do not
        # replay those human rows as live user messages; preserved tool context
        # is an API concern, not a new chat turn.
        current = current[latest_boundary + 1:]
    messages = []
    seen = set()
    for event in current:
        if event.get("type") != "message":
            continue
        uuid_value = _uuid_key(event)
        if uuid_value:
            if uuid_value in seen:
                continue
            seen.add(uuid_value)
        origin = event.get("origin") or {}
        if isinstance(origin, dict) and origin.get("kind") == "tool_result":
            continue
        text = str(event.get("text") or "")
        if not text:
            text = "\n".join(
                str(block.get("text") or "")
                for block in _content_blocks(event.get("blocks", ""))
                if block.get("type") == "text"
            ).strip()
        if text:
            messages.append({"role": str(event.get("role") or "user"), "content": text})
    return messages
