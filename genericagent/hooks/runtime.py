"""Hook registry runtime: filtering, execution, aggregation, and audit."""

from __future__ import annotations

import os
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from genericagent import paths
from genericagent.hooks.audit import HookAudit
from genericagent.hooks.config import HookRegistry, load_hook_registry
from genericagent.hooks.matcher import entry_matches, handler_matches
from genericagent.hooks.models import (
    EVENT_NAMES,
    GUARD_EVENTS,
    STOP_LIKE_EVENTS,
    HookAggregate,
    HookPayload,
    HookResult,
)
from genericagent.hooks.runner import execute_handler


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _default_prompt_runner(agent):
    """Build an isolated, tool-free model runner for prompt/agent hooks."""
    try:
        from genericagent.workflow.llm import binding_from_agent, make_tool_client
        binding = binding_from_agent(agent)
    except Exception:
        return None

    def run(kind: str, prompt: str, payload: dict[str, Any]) -> str:
        client = make_tool_client(binding)
        system = (
            "You are executing a GenericAgent hook. Follow the hook instructions. "
            "Return either a JSON output object or concise additional context."
        )
        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]
        chunks: list[str] = []
        try:
            gen = client.chat(messages=messages, tools=None)
            response = None
            while True:
                try:
                    chunks.append(str(next(gen)))
                except StopIteration as stop:
                    response = stop.value
                    break
        finally:
            try:
                client.cancel_current_request()
            except Exception:
                pass
        if response is not None and getattr(response, "content", None):
            return str(response.content)
        return "".join(chunks)
    return run


class HookRuntime:
    def __init__(
        self,
        registry: HookRegistry | None = None,
        *,
        audit: HookAudit | None = None,
        prompt_runner: Callable | None = None,
        mcp_call: Callable | None = None,
    ):
        self.registry = registry or load_hook_registry()
        self.audit = audit or HookAudit()
        self.prompt_runner = prompt_runner
        self.mcp_call = mcp_call
        self._once_seen: set[str] = set()
        self._lock = threading.Lock()
        self.session_started = False
        self.last_errors: list[dict[str, Any]] = []

    @classmethod
    def from_agent(cls, agent) -> "HookRuntime":
        return cls(
            load_hook_registry(project_root=paths.PROJECT_ROOT),
            prompt_runner=_default_prompt_runner(agent),
        )

    def _payload(self, agent, event: str, data: dict[str, Any] | None) -> HookPayload:
        payload = {
            "schema_version": 1,
            "hook_event_name": event,
            "session_id": getattr(agent, "session_id", None),
            "transcript_path": getattr(agent, "session_path", None),
            "cwd": getattr(agent, "cwd", None) or os.getcwd(),
            "permission_mode": getattr(agent, "permission_mode", None),
            "turn_id": getattr(agent, "session_turn_id", 0),
            "timestamp": _now_iso(),
            "agent_id": getattr(agent, "agent_id", None),
            "agent_type": getattr(agent, "agent_type", None),
        }
        payload.update(data or {})
        return HookPayload(event=event, data=payload)

    def _emit(self, emit, event_type: str, **fields) -> None:
        if emit is None:
            return
        try:
            emit({"type": event_type, **fields})
        except Exception:
            pass

    def status_snapshot(self, *, recent: int = 30) -> dict[str, Any]:
        events = self.event_status()
        configured = []
        for entry in self.registry.entries:
            for handler in entry.handlers:
                configured.append({
                    "event": entry.event,
                    "matcher": entry.matcher or "",
                    "type": handler.kind,
                    "source": entry.source,
                    "path": entry.source_path,
                    "statusMessage": handler.status_message,
                    "timeout": handler.timeout,
                })
        return {
            "files": list(self.registry.files),
            "entries": len(self.registry.entries),
            "handlers": len(configured),
            "handlerTypes": sorted({item["type"] for item in configured}),
            "configured": configured,
            "events": events,
            "issues": [
                {"source": issue.source, "path": issue.path, "message": issue.message}
                for issue in self.registry.issues
            ],
            "recent": self.recent_events(limit=recent),
        }

    def event_status(self) -> dict[str, str]:
        """Return CC event vocabulary annotated with GA integration status."""
        emitted = {
            "PreToolUse": "integrated",
            "PostToolUse": "integrated",
            "PostToolUseFailure": "integrated",
            "PostToolBatch": "integrated",
            "Notification": "contract-only",
            "UserPromptSubmit": "integrated",
            "UserPromptExpansion": "contract-only",
            "SessionStart": "integrated",
            "SessionEnd": "integrated",
            "Stop": "integrated",
            "StopFailure": "integrated",
            "SubagentStart": "integrated",
            "SubagentStop": "integrated",
            "PreCompact": "integrated",
            "PostCompact": "integrated",
            "PreModelSwitch": "integrated",
            "PostModelSwitch": "integrated",
            "PermissionRequest": "contract-only",
            "PermissionDenied": "integrated",
            "Setup": "contract-only",
            "TeammateIdle": "integrated",
            "TaskCreated": "integrated",
            "TaskCompleted": "integrated",
            "Elicitation": "contract-only",
            "ElicitationResult": "contract-only",
            "ConfigChange": "contract-only",
            "WorktreeCreate": "contract-only",
            "WorktreeRemove": "contract-only",
            "InstructionsLoaded": "contract-only",
            "CwdChanged": "contract-only",
            "FileChanged": "contract-only",
            "DirectoryAdded": "contract-only",
            "MessageDisplay": "contract-only",
        }
        return {event: emitted.get(event, "contract-only") for event in sorted(EVENT_NAMES)}

    def run_event(self, agent, event: str, data: dict[str, Any] | None = None, *, emit=None) -> HookAggregate:
        aggregate = HookAggregate(event=event)
        if event not in EVENT_NAMES:
            return aggregate
        payload = self._payload(agent, event, data)
        payload_dict = payload.as_dict()
        for entry in self.registry.for_event(event):
            if not entry_matches(entry, payload):
                continue
            for handler_index, handler in enumerate(entry.handlers):
                if not handler_matches(handler, payload):
                    continue
                once_key = f"{entry.source_path}:{entry.entry_index}:{handler_index}"
                with self._lock:
                    if handler.once and once_key in self._once_seen:
                        continue
                result = HookResult(
                    event=event,
                    handler=handler,
                    source=entry.source,
                    source_path=entry.source_path,
                    hook_run_id="hook_" + uuid.uuid4().hex,
                    status="running",
                )
                self._emit(
                    emit,
                    "hook_started",
                    hookRunId=result.hook_run_id,
                    event=event,
                    source=entry.source,
                    handlerType=handler.kind,
                    statusMessage=handler.status_message,
                )
                execute_handler(
                    handler,
                    payload_dict,
                    result,
                    prompt_runner=self.prompt_runner,
                    mcp_call=self.mcp_call,
                )
                # A failed command/HTTP/prompt guard can fail closed when the
                # config explicitly says onFailure=block. Stop-like events are
                # notification-first in GA v1 so a broken beep cannot trap the
                # user inside a completed turn.
                if (
                    handler.on_failure == "block"
                    and event in GUARD_EVENTS
                    and result.status in {"error", "timeout", "invalid_output"}
                ):
                    result.status = "blocking"
                    result.blocking = True
                    result.output = {"decision": "block", "reason": result.error or "hook failed"}
                if handler.once:
                    with self._lock:
                        self._once_seen.add(once_key)
                try:
                    self.audit.record(result, payload=payload_dict)
                except Exception:
                    pass
                self._emit(
                    emit,
                    "hook_response" if result.status in {"success", "blocking"} else "hook_error",
                    hookRunId=result.hook_run_id,
                    event=event,
                    source=entry.source,
                    handlerType=handler.kind,
                    status=result.status,
                    durationMs=result.duration_ms,
                    message=result.error,
                )
                if result.status not in {"success", "blocking"}:
                    self.last_errors.append({
                        "hookRunId": result.hook_run_id,
                        "event": event,
                        "source": entry.source,
                        "handlerType": handler.kind,
                        "status": result.status,
                        "message": result.error,
                        "durationMs": result.duration_ms,
                    })
                    self.last_errors = self.last_errors[-50:]
                aggregate.add(result)
                if aggregate.blocked and event in GUARD_EVENTS:
                    return aggregate
        return aggregate

    def recent_events(self, limit: int = 50) -> list[dict[str, Any]]:
        return self.audit.recent_events(limit=limit)


def hook_runtime_for_agent(agent) -> HookRuntime:
    runtime = getattr(agent, "hook_runtime", None)
    if isinstance(runtime, HookRuntime):
        return runtime
    runtime = HookRuntime.from_agent(agent)
    setattr(agent, "hook_runtime", runtime)
    return runtime


def run_hook_event(agent, event: str, data: dict[str, Any] | None = None, *, emit=None) -> HookAggregate:
    try:
        return hook_runtime_for_agent(agent).run_event(agent, event, data, emit=emit)
    except Exception:
        return HookAggregate(event=event)
