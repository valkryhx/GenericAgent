from __future__ import annotations

import json
import re
from pathlib import Path

from subagent_state import atomic_write_json, now_iso


_SECRET_RE = re.compile(r"(?i)(api[_-]?key|token|secret|password)=([^\s,;]+)")


class SubagentTranscriptStore:
    def __init__(self, sessions_dir):
        self.sessions_dir = Path(sessions_dir)

    def write_metadata(self, *, session_id, run_id, agent_path, **extra):
        path = self._meta_path(session_id, run_id)
        data = {
            "schema_version": 1,
            "type": "metadata",
            "session_id": session_id,
            "run_id": run_id,
            "agent_path": agent_path,
            "created_at": now_iso(),
            **extra,
        }
        atomic_write_json(path, data)
        self.append_event(session_id, run_id, "metadata", data)
        return data

    def append_event(self, session_id, run_id, event_type, payload):
        path = self._events_path(session_id, run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        row = {
            "schema_version": 1,
            "type": event_type,
            "created_at": now_iso(),
            "payload": _redact(payload),
        }
        with open(path, "a", encoding="utf-8", newline="\n") as f:
            f.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        return row

    def build_resume_context(self, session_id, run_id, edits=None):
        summary = self.replay(session_id, run_id)
        if edits:
            timeline = self.build_replay_timeline(session_id, run_id)
            history = []
            applied = 0
            edits = dict(edits or {})
            for item in timeline:
                role = item.get("resume_role")
                content = item.get("resume_content")
                if not role or content is None:
                    continue
                edit = edits.get(item.get("event_key")) or {}
                if edit.get("drop"):
                    applied += 1
                    continue
                if "resume_content" in edit:
                    content = str(edit.get("resume_content") or "")
                    applied += 1
                history.append({"role": role, "content": content})
            final_output = summary.get("final_output") or {}
            final_text = _payload_text(final_output, "final_output") or _payload_text(final_output, "content") or _payload_text(final_output, "text")
            closed = summary.get("closed")
            terminal = bool(closed or final_output)
            return {
                "schema_version": 1,
                "status": "terminal" if terminal else "resumable",
                "session_id": summary.get("session_id"),
                "run_id": summary.get("run_id"),
                "agent_path": summary.get("agent_path"),
                "request_prompt": _payload_text(summary.get("request") or {}, "prompt") or _payload_text(summary.get("request") or {}, "message"),
                "final_output": final_text,
                "backend_history": history,
                "can_continue_turn": not terminal,
                "source_event_count": summary.get("event_count", 0),
                "invalid_event_count": summary.get("invalid_event_count", 0),
                "timeline_event_count": len(timeline),
                "applied_edit_count": applied,
            }
        history = []
        request = summary.get("request") or {}
        prompt = _payload_text(request, "prompt") or _payload_text(request, "message")
        if prompt:
            history.append({"role": "user", "content": prompt})
        tool_calls = summary.get("tool_calls") or []
        tool_results = summary.get("tool_results") or []
        for index, call in enumerate(tool_calls):
            history.append({"role": "assistant", "content": _tool_call_resume_text(call)})
            if index < len(tool_results):
                history.append({"role": "user", "content": _tool_result_resume_text(tool_results[index])})
        for message in summary.get("assistant_messages") or []:
            content = _payload_text(message, "content") or _payload_text(message, "message") or _payload_text(message, "text")
            if content:
                history.append({"role": "assistant", "content": content})
        final_output = summary.get("final_output") or {}
        final_text = _payload_text(final_output, "final_output") or _payload_text(final_output, "content") or _payload_text(final_output, "text")
        if final_text:
            history.append({"role": "assistant", "content": f"[GA_SUBAGENT_FINAL_OUTPUT]\n{final_text}"})
        closed = summary.get("closed")
        terminal = bool(closed or final_output)
        return {
            "schema_version": 1,
            "status": "terminal" if terminal else "resumable",
            "session_id": summary.get("session_id"),
            "run_id": summary.get("run_id"),
            "agent_path": summary.get("agent_path"),
            "request_prompt": prompt,
            "final_output": final_text,
            "backend_history": history,
            "can_continue_turn": not terminal,
            "source_event_count": summary.get("event_count", 0),
            "invalid_event_count": summary.get("invalid_event_count", 0),
        }

    def replay(self, session_id, run_id):
        path = self._events_path(session_id, run_id)
        meta = self._read_meta(session_id, run_id)
        summary = {
            "schema_version": 1,
            "session_id": str(session_id),
            "run_id": str(run_id),
            "agent_path": meta.get("agent_path"),
            "metadata": meta or None,
            "event_count": 0,
            "invalid_event_count": 0,
            "last_event_type": None,
            "request": None,
            "permission_decisions": [],
            "tool_calls": [],
            "tool_results": [],
            "assistant_messages": [],
            "final_output": None,
            "closed": None,
            "events": [],
        }
        if not path.exists():
            return summary
        with open(path, encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except Exception:
                    summary["invalid_event_count"] += 1
                    continue
                event_type = row.get("type")
                payload = row.get("payload") or {}
                summary["event_count"] += 1
                summary["last_event_type"] = event_type
                summary["events"].append(row)
                if event_type == "metadata":
                    summary["metadata"] = payload
                    summary["agent_path"] = payload.get("agent_path") or summary.get("agent_path")
                elif event_type == "request":
                    summary["request"] = payload
                elif event_type == "permission_decision":
                    summary["permission_decisions"].append(payload)
                elif event_type == "tool_call":
                    summary["tool_calls"].append(payload)
                elif event_type == "tool_result":
                    summary["tool_results"].append(payload)
                elif event_type == "assistant":
                    summary["assistant_messages"].append(payload)
                elif event_type == "final_output":
                    summary["final_output"] = payload
                elif event_type == "agent_closed":
                    summary["closed"] = payload
        return summary

    def build_replay_timeline(self, session_id, run_id):
        summary = self.replay(session_id, run_id)
        timeline = []

        def add_item(event_type, resume_role=None, resume_content=None, editable=True, payload=None):
            event_key = f"{len(timeline)}:{event_type}"
            timeline.append(
                {
                    "index": len(timeline),
                    "event_key": event_key,
                    "type": event_type,
                    "editable": bool(editable and resume_role is not None),
                    "resume_role": resume_role,
                    "resume_content": resume_content,
                    "payload": payload or {},
                }
            )

        metadata = summary.get("metadata")
        if metadata is not None or summary.get("agent_path"):
            add_item("metadata", resume_role=None, resume_content=None, editable=False, payload=metadata or {})

        request = summary.get("request") or {}
        prompt = _payload_text(request, "prompt") or _payload_text(request, "message")
        if prompt:
            add_item("request", resume_role="user", resume_content=prompt, payload=request)

        tool_calls = summary.get("tool_calls") or {}
        tool_results = summary.get("tool_results") or []
        for index, call in enumerate(tool_calls):
            add_item("tool_call", resume_role="assistant", resume_content=_tool_call_resume_text(call), payload=call)
            if index < len(tool_results):
                result = tool_results[index]
                add_item("tool_result", resume_role="user", resume_content=_tool_result_resume_text(result), payload=result)

        for message in summary.get("assistant_messages") or []:
            content = _payload_text(message, "content") or _payload_text(message, "message") or _payload_text(message, "text")
            if content:
                add_item("assistant", resume_role="assistant", resume_content=content, payload=message)

        final_output = summary.get("final_output") or {}
        final_text = _payload_text(final_output, "final_output") or _payload_text(final_output, "content") or _payload_text(final_output, "text")
        if final_text:
            add_item("final_output", resume_role="assistant", resume_content=f"[GA_SUBAGENT_FINAL_OUTPUT]\n{final_text}", payload=final_output)

        return timeline

    def _read_meta(self, session_id, run_id):
        path = self._meta_path(session_id, run_id)
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                return json.load(f)
        except Exception:
            return {}

    def _events_path(self, session_id, run_id):
        return self.sessions_dir / str(session_id) / "subagents" / f"{run_id}.jsonl"

    def _meta_path(self, session_id, run_id):
        return self.sessions_dir / str(session_id) / "subagents" / f"{run_id}.meta.json"


def _payload_text(payload, key):
    value = payload.get(key) if isinstance(payload, dict) else None
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _tool_call_resume_text(payload):
    tool_name = payload.get("tool_name") or payload.get("name") or "tool"
    args = payload.get("args") or payload.get("arguments") or {}
    return f"[GA_SUBAGENT_TOOL_CALL] {tool_name} {json.dumps(args, ensure_ascii=False, separators=(',', ':'))}"


def _tool_result_resume_text(payload):
    tool_name = payload.get("tool_name") or payload.get("name") or "tool"
    status = payload.get("status") or payload.get("action") or "result"
    result = payload.get("result")
    if result is None:
        result = payload.get("content")
    if result is None:
        result = payload.get("output")
    if result is None:
        result = payload
    if not isinstance(result, str):
        result = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
    return f"[GA_SUBAGENT_TOOL_RESULT] {tool_name} status={status}\n{result}"


def _redact(value):
    if isinstance(value, dict):
        return {key: _redact(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    if isinstance(value, str):
        return _SECRET_RE.sub(lambda m: f"{m.group(1)}=<redacted>", value)
    return value
