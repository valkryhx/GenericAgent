"""Hook registry runtime: filtering, execution, aggregation, and audit."""

from __future__ import annotations

import os
import queue
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from genericagent import paths
from genericagent.hooks.audit import HookAudit
from genericagent.hooks.config import (
    HookRegistry,
    load_hook_registry,
    parse_dynamic_handler,
)
from genericagent.hooks.matcher import entry_matches, handler_matches
from genericagent.hooks.models import (
    CONFIG_GUARD_EVENTS,
    EVENT_NAMES,
    GUARD_EVENTS,
    STOP_LIKE_EVENTS,
    HookAggregate,
    HookEntry,
    HookHandler,
    HookPayload,
    HookResult,
    HookWakeRequest,
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
        # Session-scoped dynamic hooks (goal / mods). Keyed for idempotent
        # replacement; insertion order is the execution order after static
        # entries. Not shared across agents: one dict per HookRuntime.
        self._session_hooks: dict[str, HookEntry] = {}
        self.last_errors: list[dict[str, Any]] = []
        self._async_threads: set[threading.Thread] = set()
        self._async_requests: queue.Queue[HookWakeRequest] = queue.Queue()
        self._wake_handler: Callable[[HookWakeRequest], bool] | None = None
        self._wake_thread: threading.Thread | None = None
        self._wake_stop = threading.Event()

    @classmethod
    def from_agent(cls, agent) -> "HookRuntime":
        return cls(
            load_hook_registry(project_root=paths.PROJECT_ROOT),
            prompt_runner=_default_prompt_runner(agent),
        )

    def set_wake_handler(self, handler: Callable[[HookWakeRequest], bool] | None) -> None:
        """Install the host callback that turns asyncRewake into a new turn."""
        self._wake_handler = handler
        if handler is not None and (self._wake_thread is None or not self._wake_thread.is_alive()):
            self._wake_stop.clear()
            self._wake_thread = threading.Thread(
                target=self._wake_loop,
                daemon=True,
                name="ga-hook-wake",
            )
            self._wake_thread.start()

    def _wake_loop(self) -> None:
        while not self._wake_stop.is_set():
            try:
                request = self._async_requests.get(timeout=0.2)
            except queue.Empty:
                continue
            try:
                handler = self._wake_handler
                if handler is not None:
                    handler(request)
            except Exception:
                pass
            finally:
                self._async_requests.task_done()

    def _queue_wake(self, request: HookWakeRequest) -> None:
        self._async_requests.put(request)

    def close(self) -> None:
        self._wake_stop.set()
        thread = self._wake_thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=1.0)
        self._wake_thread = None

    def _complete_async(
        self,
        result: HookResult,
        payload: dict[str, Any],
        emit,
    ) -> None:
        """Persist and announce one background handler completion."""
        try:
            self.audit.record(result, payload=payload)
        except Exception as audit_error:
            self._emit(
                emit,
                "hook_audit_error",
                hookRunId=result.hook_run_id,
                event=result.event,
                source=result.source,
                handlerType=result.handler.kind,
                status=result.status,
                message=f"audit write failed: {type(audit_error).__name__}: {audit_error}",
            )
        self._emit(
            emit,
            "async_hook_response" if result.status in {"success", "blocking"} else "hook_error",
            hookRunId=result.hook_run_id,
            event=result.event,
            source=result.source,
            handlerType=result.handler.kind,
            status=result.status,
            durationMs=result.duration_ms,
            message=result.error,
            backgrounded=True,
        )
        if result.status not in {"success", "blocking"}:
            with self._lock:
                self.last_errors.append({
                    "hookRunId": result.hook_run_id,
                    "event": result.event,
                    "source": result.source,
                    "handlerType": result.handler.kind,
                    "status": result.status,
                    "message": result.error,
                    "durationMs": result.duration_ms,
                })
                self.last_errors = self.last_errors[-50:]
        if result.handler.async_rewake and result.exit_code == 2:
            body = (result.stderr or result.stdout or "").strip()
            prefix = result.handler.rewake_message or (
                f'{result.event} hook blocking error from command "{result.handler.kind}":'
            )
            summary = re.sub(r"\s+", " ", result.handler.rewake_summary).strip()[:300] or "Stop hook feedback"
            self._queue_wake(HookWakeRequest(
                event=result.event,
                hook_run_id=result.hook_run_id,
                hook_name=result.handler.kind,
                message=(prefix + " " + body).strip(),
                summary=summary,
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.exit_code,
                session_id=str(payload.get("session_id") or ""),
                agent_id=str(payload.get("agent_id") or ""),
            ))

    def _run_async(
        self,
        handler,
        payload: dict[str, Any],
        result: HookResult,
        emit,
    ) -> None:
        def worker() -> None:
            try:
                execute_handler(
                    handler,
                    payload,
                    result,
                    prompt_runner=self.prompt_runner,
                    mcp_call=self.mcp_call,
                    allowed_http_urls=self.registry.allowed_http_hook_urls,
                    global_allowed_env_vars=self.registry.http_hook_allowed_env_vars,
                )
                result.backgrounded = True
                self._complete_async(result, payload, emit)
            finally:
                with self._lock:
                    self._async_threads.discard(threading.current_thread())
        thread = threading.Thread(target=worker, daemon=True, name=f"ga-hook-async-{result.hook_run_id}")
        with self._lock:
            self._async_threads.add(thread)
        thread.start()

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
                    "async": handler.async_requested,
                    "asyncRewake": handler.async_rewake,
                })
        return {
            "files": list(self.registry.files),
            "entries": len(self.registry.entries),
            "handlers": len(configured),
            "handlerTypes": sorted({item["type"] for item in configured}),
            "httpPolicy": {
                "allowedUrlsConfigured": self.registry.allowed_http_hook_urls is not None,
                "allowedUrlCount": len(self.registry.allowed_http_hook_urls or []),
                "allowedEnvVarsConfigured": self.registry.http_hook_allowed_env_vars is not None,
                "allowedEnvVarCount": len(self.registry.http_hook_allowed_env_vars or []),
            },
            "configured": configured,
            "sessionHooks": self.list_session_hooks(),
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
            "Notification": "integrated",
            "UserPromptSubmit": "integrated",
            "UserPromptExpansion": "integrated",
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
            "PermissionRequest": "integrated",
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
            "MessageDisplay": "integrated",
        }
        return {event: emitted.get(event, "contract-only") for event in sorted(EVENT_NAMES)}

    def _run_entries(self, entries, event, payload, payload_dict, aggregate, emit, *, dynamic):
        """Execute a list of HookEntry objects against one payload.

        ``dynamic`` marks session-scoped hooks; those may never grant an
        ``allow`` verdict on a guard event (they can only tighten: deny/ask or
        add context), so a runtime-registered hook cannot escalate privileges.
        """
        for entry in entries:
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
                if handler.async_requested:
                    # CC config-based async returns a backgrounded response
                    # immediately; completion arrives on a separate channel.
                    result.backgrounded = True
                    if handler.once:
                        with self._lock:
                            self._once_seen.add(once_key)
                    self._run_async(handler, payload_dict, result, emit)
                    aggregate.add(result)
                    continue
                execute_handler(
                    handler,
                    payload_dict,
                    result,
                    prompt_runner=self.prompt_runner,
                    mcp_call=self.mcp_call,
                    allowed_http_urls=self.registry.allowed_http_hook_urls,
                    global_allowed_env_vars=self.registry.http_hook_allowed_env_vars,
                )
                # Failure->block promotion now lives in runner.execute_handler
                # (_apply_failure_policy) so all handler kinds share one rule.
                if handler.once:
                    with self._lock:
                        self._once_seen.add(once_key)
                try:
                    self.audit.record(result, payload=payload_dict)
                except Exception as audit_error:
                    self._emit(
                        emit,
                        "hook_audit_error",
                        hookRunId=result.hook_run_id,
                        event=event,
                        source=entry.source,
                        handlerType=handler.kind,
                        status=result.status,
                        message=f"audit write failed: {type(audit_error).__name__}: {audit_error}",
                    )
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
                # Security boundary: dynamic (session) hooks cannot widen
                # permissions on guard events. A forged allow is downgraded to
                # ask so the host still decides; deny/ask/context pass through.
                if dynamic and event in GUARD_EVENTS:
                    result = self._neutralize_dynamic_allow(result)
                aggregate.add(result)
                if aggregate.blocked and event in GUARD_EVENTS:
                    return aggregate
        return aggregate

    def _neutralize_dynamic_allow(self, result: HookResult) -> HookResult:
        # PreToolUse stores allow in permissionDecision; PermissionRequest
        # stores it in nested decision.behavior. Cover both.
        if result.permission_decision != 'allow' and result.permission_behavior != 'allow':
            return result
        output = result.output_dict
        specific = output.get('hookSpecificOutput')
        if isinstance(specific, dict):
            if specific.get('permissionDecision') == 'allow':
                specific['permissionDecision'] = 'ask'
            decision = specific.get('decision')
            if isinstance(decision, dict) and decision.get('behavior') == 'allow':
                decision['behavior'] = 'ask'
        self.last_errors.append({
            'hookRunId': result.hook_run_id,
            'event': result.event,
            'source': result.source,
            'handlerType': result.handler.kind,
            'status': 'downgraded',
            'message': 'dynamic session hook may not grant allow on a guard event; downgraded to ask',
            'durationMs': result.duration_ms,
        })
        self.last_errors = self.last_errors[-50:]
        return result

    # ---- session-scoped dynamic hooks (goal / mods) -------------------

    def add_session_hook(self, event: str, handler_spec, *, key: str | None = None, matcher: str | None = None,
                         emit=None) -> str:
        """Register (or replace) one session-scoped hook.

        ``handler_spec`` is either a ``HookHandler`` or a raw config dict (the
        same shape used in ``hooks.json``). ``key`` makes it idempotent: adding
        again with the same key replaces the previous entry in place, so goal
        re-installing ``goal:stop`` never stacks duplicates. Returns the key.

        Dynamic hooks run after static file hooks and, on guard events, can
        only tighten (deny/ask/context) - never grant ``allow`` (see
        ``_neutralize_dynamic_allow``).
        """
        if event not in EVENT_NAMES:
            raise ValueError(f'unknown hook event: {event}')
        if isinstance(handler_spec, HookHandler):
            handler = handler_spec
        else:
            handler = parse_dynamic_handler(handler_spec)
        if key is None or not str(key).strip():
            key = 'session:' + uuid.uuid4().hex
        key = str(key)
        with self._lock:
            if key in self._session_hooks:
                index = self._session_hooks[key].entry_index
            else:
                index = len(self._session_hooks)
            self._session_hooks[key] = HookEntry(
                event=event,
                matcher=matcher,
                handlers=(handler,),
                source='session',
                source_path=f'session:{key}',
                source_rank=900,
                entry_index=index,
            )
        self._emit(emit, 'session_hook_registered', event=event, key=key, handlerType=handler.kind)
        return key

    def remove_session_hook(self, key: str) -> bool:
        with self._lock:
            return self._session_hooks.pop(str(key), None) is not None

    def list_session_hooks(self, event: str | None = None) -> list[dict[str, Any]]:
        out = []
        for key, entry in self._session_hooks.items():
            if event is not None and entry.event != event:
                continue
            for handler in entry.handlers:
                out.append({
                    'key': key,
                    'event': entry.event,
                    'matcher': entry.matcher or '',
                    'type': handler.kind,
                    'timeout': handler.timeout,
                })
        return out

    def clear_session_hooks(self) -> int:
        """Drop all dynamic hooks (session end / agent teardown)."""
        with self._lock:
            count = len(self._session_hooks)
            self._session_hooks.clear()
        return count

    def run_event(self, agent, event: str, data: dict[str, Any] | None = None, *, emit=None) -> HookAggregate:
        aggregate = HookAggregate(event=event)
        if event not in EVENT_NAMES:
            return aggregate
        payload = self._payload(agent, event, data)
        payload_dict = payload.as_dict()
        # A guard event whose config could not be loaded is fail-closed: a bad
        # guard may be the very permission gate, so we block rather than run a
        # partial/empty guard. Non-guard events ignore config errors.
        config_errors = self.registry.guard_config_errors.get(event) if event in CONFIG_GUARD_EVENTS else None
        if config_errors:
            aggregate.add(self._config_error_result(event, config_errors))
            if aggregate.blocked:
                return aggregate
        # Static entries run first, then session-scoped dynamic entries (CC
        # ordering). Each pass stops early on a blocking guard verdict.
        self._run_entries(self.registry.for_event(event), event, payload, payload_dict, aggregate, emit, dynamic=False)
        if not (aggregate.blocked and event in GUARD_EVENTS):
            # Session-scoped Stop hooks (e.g. goal) only apply to the turn-end
            # gate, never the app-level user_turn_end fallback. Both phases emit
            # the same "Stop" event, so without this the goal evaluator would
            # run twice per turn (and iterations would double-count). Static
            # Stop hooks such as a beep are unaffected.
            skip_dynamic = event == "Stop" and payload_dict.get("phase") == "user_turn_end"
            dynamic_entries = [] if skip_dynamic else [e for e in self._session_hooks.values() if e.event == event]
            if dynamic_entries:
                self._run_entries(dynamic_entries, event, payload, payload_dict, aggregate, emit, dynamic=True)
        return aggregate

    def _config_error_result(self, event: str, messages: list[str]) -> HookResult:
        handler = HookHandler(kind="command", options={"type": "command", "command": "<invalid-guard-config>"})
        detail = "; ".join(str(m) for m in messages)
        reason = "Guard hook configuration could not be loaded; blocking to stay fail-closed. " + detail
        result = HookResult(
            event=event, handler=handler, source="config", source_path="config:guard-error",
            hook_run_id="hook_cfg_" + uuid.uuid4().hex, status="blocking", blocking=True,
            error=detail, output={"decision": "block", "reason": reason},
        )
        if event == "PermissionRequest":
            result.output = {"hookSpecificOutput": {
                "hookEventName": "PermissionRequest",
                "decision": {"behavior": "deny", "message": reason},
            }}
        return result

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
