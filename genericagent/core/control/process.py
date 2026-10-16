from __future__ import annotations

import copy
import json
from dataclasses import replace
from pathlib import Path

from agent_runtime_models import (
    AgentCapabilities,
    AgentEvent,
    AgentEventBatch,
    AgentRecord,
    AgentResultRecord,
    ArtifactRef,
    make_process_execution_id,
)
from sensitive_redaction import redact_sensitive_text, sanitize
from subagent_agent_path import AgentPath
from subagent_state import read_json_or_none


_TERMINAL_STATUSES = frozenset({"succeeded", "failed", "cancelled", "killed", "interrupted", "closed", "cached", "stale", "partial"})
_ACTIONS = frozenset(
    {
        "read",
        "events",
        "result",
        "artifacts",
        "interrupt",
        "close",
        "message",
        "followup",
        "resume",
        "attach",
        "detach",
    }
)


class ProcessSubagentAdapter:
    engine = "process"
    source_cursor = "process"

    def __init__(self, manager):
        self.manager = manager

    def list_records(self, *, include_terminal: bool = False, path_prefix: str | None = None) -> list[AgentRecord]:
        states = self.manager.list_agent_snapshots(
            path_prefix=path_prefix,
            include_closed=include_terminal,
        )
        records = [self._record_from_state(state) for state in states]
        if not include_terminal:
            records = [record for record in records if record.status not in _TERMINAL_STATUSES]
        if path_prefix:
            records = [record for record in records if record.agent_path and record.agent_path.startswith(path_prefix)]
        return records

    def get_record(self, execution_id: str) -> AgentRecord | None:
        return next(
            (record for record in self.list_records(include_terminal=True) if record.execution_id == execution_id),
            None,
        )

    def events_since(self, cursors: dict[str, int] | None = None, *, execution_id: str | None = None) -> AgentEventBatch:
        cursor = int((cursors or {}).get(self.source_cursor, 0) or 0)
        try:
            raw_events = self.manager.event_bus.read_events_since(cursor)
        except Exception as exc:
            return AgentEventBatch(next_cursors={self.source_cursor: cursor}, errors={self.source_cursor: redact_sensitive_text(str(exc))})
        events: list[AgentEvent] = []
        next_cursor = cursor
        states = self.manager.list_agent_snapshots(include_closed=True)
        state_by_execution = {
            self._execution_id(state): state
            for state in states
        }
        for raw in raw_events:
            event = AgentEvent.from_subagent_event(raw, execution_id=self._execution_id_for_raw(raw, state_by_execution))
            next_cursor = max(next_cursor, int(event.source_sequence or event.sequence or 0))
            if execution_id and event.execution_id != execution_id:
                continue
            if event.artifact_ref:
                state = state_by_execution.get(event.execution_id)
                artifact = self._final_artifact(state) if state else None
                if artifact and artifact.artifact_id == event.artifact_ref.artifact_id:
                    event = replace(event, artifact_ref=artifact)
            events.append(event)
        return AgentEventBatch(events=tuple(events), next_cursors={self.source_cursor: next_cursor})

    def read_result(self, execution_id: str, *, include_preview: bool = False) -> AgentResultRecord:
        state = self._state_for_execution(execution_id)
        if state is None:
            return AgentResultRecord(execution_id=execution_id, status="unknown", error="not_found")
        record = self._record_from_state(state)
        payload = {}
        if include_preview and record.final_text_ref and record.final_text_ref.ref:
            try:
                with Path(record.final_text_ref.ref).open("r", encoding="utf-8", errors="replace") as fh:
                    payload = {"preview": " ".join(fh.read(240).split())[:240]}
            except OSError:
                payload = {}
        return AgentResultRecord(
            execution_id=execution_id,
            status=record.status,
            payload=payload,
            final_text_ref=record.artifact_refs[0] if record.artifact_refs else None,
            transcript_ref=record.transcript_ref,
            error=record.error,
        )

    def control(self, execution_id: str, request):
        from agent_control import ControlResult

        record = self.get_record(execution_id)
        if record is None:
            return ControlResult(ok=False, code="not_found", execution_id=execution_id)
        action = getattr(request, "action", "")
        if not record.capabilities.supports_action(action):
            return ControlResult(
                ok=False,
                code="unsupported_capability",
                execution_id=execution_id,
                scope="execution",
                status=record.status,
                data={"requestedAction": action, "capabilities": sorted(record.capabilities.actions)},
            )
        reason = getattr(request, "reason", "") or ""
        payload = getattr(request, "payload", {}) or {}
        target = record.agent_path
        try:
            if action == "interrupt":
                self.manager.interrupt_agent(target, reason=reason or "parent_interrupt")
                data = {}
            elif action == "close":
                cascade = payload.get("cascade")
                if not isinstance(cascade, bool):
                    return ControlResult(ok=False, code="invalid_request", execution_id=execution_id, scope="execution", status=record.status)
                closed = self.manager.close_agent(target, reason=reason or "parent_cleanup", cascade=cascade)
                descendants = closed.get("closedDescendantExecutionIds", []) if isinstance(closed, dict) else []
                if not isinstance(closed, dict):
                    descendants = self._close_descendant_execution_ids(closed)
                data = {"closedDescendantExecutionIds": descendants}
            elif action in {"message", "followup", "resume"}:
                message = payload.get("message")
                if not isinstance(message, str) or not message.strip():
                    return ControlResult(ok=False, code="invalid_request", execution_id=execution_id, scope="execution", status=record.status)
                method = {
                    "message": self.manager.send_message,
                    "followup": self.manager.followup_task,
                    "resume": self.manager.resume_agent,
                }[action]
                method(target, message)
                data = {}
            elif action in {"attach", "detach"}:
                since_offset = payload.get("sinceOffset", 0)
                max_chars = payload.get("maxChars")
                if not isinstance(since_offset, int) or since_offset < 0:
                    return ControlResult(ok=False, code="invalid_request", execution_id=execution_id, scope="execution", status=record.status)
                if max_chars is not None and (not isinstance(max_chars, int) or max_chars < 0):
                    return ControlResult(ok=False, code="invalid_request", execution_id=execution_id, scope="execution", status=record.status)
                method = self.manager.attach_agent if action == "attach" else self.manager.detach_agent
                method(target, since_offset=since_offset, max_chars=max_chars, reason=reason or f"parent_{action}")
                data = {}
            else:
                return ControlResult(ok=False, code="unsupported_capability", execution_id=execution_id, scope="execution", status=record.status)
            scope = "agent_tree" if action == "close" else "execution"
            return ControlResult(ok=True, code="ok", execution_id=execution_id, scope=scope, status=record.status, data=data)
        except Exception as exc:
            return ControlResult(
                ok=False,
                code="control_error",
                execution_id=execution_id,
                scope="execution",
                status=record.status,
                message=redact_sensitive_text(str(exc)),
            )

    def _record_from_state(self, state) -> AgentRecord:
        status = self._project_status(state)
        metadata = self._metadata_from_state(state)
        return AgentRecord(
            execution_id=self._execution_id(state),
            engine="process",
            record_kind="process_agent",
            status=status,
            source_status=state.process_status,
            agent_path=state.agent_path,
            run_id=state.run_id,
            task_name=state.task_name,
            turn_status=state.turn_status,
            process_status=state.process_status,
            workspace=state.task_dir,
            permission_profile=state.permission_profile,
            capability_snapshot={},
            artifact_refs=(self._final_artifact(state),) if self._final_artifact(state) else (),
            transcript_ref=self._transcript_ref(state),
            capabilities=AgentCapabilities(actions=_ACTIONS),
            updated_at=state.updated_at,
            error=redact_sensitive_text(state.last_error) if state.last_error else None,
            metadata=metadata,
        )

    def _metadata_from_state(self, state) -> dict:
        metadata = {
            "parentAgentPath": self._parent_agent_path(state.agent_path),
            "worktreePath": state.worktree_path,
            "isolation": state.isolation,
            "parentPermissionMode": state.parent_permission_mode,
            "permissionOptions": copy.deepcopy(state.permission_options or {}),
            "agentType": state.agent_type,
            "ipcMode": state.ipc_mode,
        }
        if state.worktree_summary is not None:
            metadata["worktreeSummary"] = copy.deepcopy(state.worktree_summary)
        return sanitize({key: value for key, value in metadata.items() if value is not None})

    def _state_for_execution(self, execution_id: str):
        for state in self.manager.list_agent_snapshots(include_closed=True):
            if self._execution_id(state) == execution_id:
                return self.manager.probe_agent(state.agent_path)
        return None

    @staticmethod
    def _project_status(state) -> str:
        if state.process_status == "killed":
            return "killed"
        if state.turn_status == "completed":
            return "succeeded"
        if state.turn_status in {"errored", "failed"}:
            return "failed"
        if state.turn_status == "interrupted":
            return "interrupted"
        if state.process_status in {"shutdown", "exited"}:
            return "closed"
        if state.turn_status in {"pending", "queued"}:
            return "queued"
        return "running"

    @staticmethod
    def _parent_agent_path(agent_path: str | None) -> str | None:
        try:
            parent = AgentPath.parse(agent_path).parent
            return str(parent)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _execution_id(state) -> str:
        return make_process_execution_id(state.run_id or state.task_name, state.agent_path)

    def _execution_id_for_raw(self, raw: dict, state_by_execution: dict[str, object]) -> str:
        run_id = raw.get("run_id")
        agent_path = raw.get("agent_path") or ""
        candidate = make_process_execution_id(run_id or raw.get("task_name"), agent_path)
        if candidate in state_by_execution:
            return candidate
        return candidate

    def _close_descendant_execution_ids(self, closed) -> list[str]:
        states_by_path = {
            state.agent_path: state
            for state in self.manager.list_agent_snapshots(include_closed=True)
            if getattr(state, "agent_path", None)
        }
        execution_ids = []
        for item in getattr(closed, "closed_descendants", []) or []:
            if not isinstance(item, dict):
                continue
            agent_path = item.get("agent_path")
            if not agent_path:
                continue
            state = states_by_path.get(agent_path)
            if state is None:
                try:
                    state = self.manager.probe_agent(agent_path)
                except Exception:
                    state = None
            run_id = getattr(state, "run_id", None) if state is not None else item.get("run_id") or item.get("runId")
            if run_id:
                execution_ids.append(make_process_execution_id(run_id, agent_path))
        return execution_ids

    def _transcript_ref(self, state) -> str | None:
        if not state.parent_session_id or not state.run_id:
            return None
        return f"sessions/{state.parent_session_id}/subagents/{state.run_id}.jsonl"

    def _final_artifact(self, state) -> ArtifactRef | None:
        artifact_dir = state.artifact_dir
        if not artifact_dir:
            return None
        manifest = read_json_or_none(Path(artifact_dir) / "artifacts.json") or {}
        artifacts = manifest.get("artifacts") if isinstance(manifest, dict) else []
        final_path = str(state.final_output_path or "")
        candidates = [item for item in (artifacts or []) if isinstance(item, dict) and item.get("artifact_id")]
        for item in reversed(candidates):
            if item.get("path") == final_path or item.get("type") == "final_output":
                return ArtifactRef(
                    artifact_id=str(item["artifact_id"]),
                    artifact_type=str(item.get("type") or "final_output"),
                    ref=item.get("path"),
                    engine="process",
                )
        return None
