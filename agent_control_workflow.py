from __future__ import annotations

import copy
import json
from pathlib import Path

from agent_runtime_models import (
    AgentCapabilities,
    AgentEvent,
    AgentEventBatch,
    AgentRecord,
    AgentResultRecord,
    ArtifactRef,
    make_workflow_child_execution_id,
    make_workflow_run_execution_id,
    make_workflow_source_cursor,
)
from sensitive_redaction import redact_sensitive_text, sanitize
from workflow_models import (
    WorkflowEvent,
    WorkflowJob,
    WorkflowRun,
    project_workflow_execution_outcome,
    summarize_workflow_jobs,
)
from workflow_store import WorkflowStore


_TERMINAL_STATUSES = frozenset(
    {"succeeded", "failed", "cancelled", "killed", "cached", "skipped", "stale", "interrupted", "closed", "partial"}
)
_RUN_STATUS_MAP = {
    "draft": "pending",
    "awaiting_approval": "queued",
    "running": "running",
    "succeeded": "succeeded",
    "completed": "succeeded",
    "failed": "failed",
    "cancelled": "cancelled",
    "killed": "killed",
    "interrupted": "interrupted",
}
_JOB_STATUS_MAP = {
    "registered": "queued",
    "queued": "queued",
    "running": "running",
    "succeeded": "succeeded",
    "failed": "failed",
    "cancelled": "cancelled",
    "killed": "killed",
    "cached": "cached",
    "skipped": "skipped",
    "stale": "stale",
}
_READ_ACTIONS = frozenset({"read", "events", "result", "artifacts"})


def _safe_metadata(value: dict | None, keys: tuple[str, ...]) -> dict:
    source = value if isinstance(value, dict) else {}
    return {key: sanitize(copy.deepcopy(source[key])) for key in keys if key in source}


def _artifact_ref(ref: str | None, *, artifact_type: str) -> tuple[ArtifactRef, ...]:
    if not ref:
        return ()
    return (ArtifactRef(artifact_id=str(ref), artifact_type=artifact_type, ref=str(ref), engine="workflow"),)


class WorkflowChildAdapter:
    engine = "workflow"

    def __init__(self, store: WorkflowStore, controller=None):
        self.store = store
        self.controller = controller

    def list_records(self, *, include_terminal: bool = False, path_prefix: str | None = None) -> list[AgentRecord]:
        records: list[AgentRecord] = []
        for run in self.store.list_runs():
            run_record, child_records = self._records_for_run(run)
            for record in (run_record, *child_records):
                if not include_terminal and record.status in _TERMINAL_STATUSES:
                    continue
                if path_prefix and not record.execution_id.startswith(path_prefix):
                    continue
                records.append(record)
        return records

    def get_record(self, execution_id: str) -> AgentRecord | None:
        return next(
            (record for record in self.list_records(include_terminal=True) if record.execution_id == execution_id),
            None,
        )

    def events_since(self, cursors: dict[str, int] | None = None, *, execution_id: str | None = None) -> AgentEventBatch:
        cursors = {str(key): int(value) for key, value in (cursors or {}).items()}
        target = self.get_record(execution_id) if execution_id else None
        if execution_id and target is None:
            return AgentEventBatch()
        events: list[AgentEvent] = []
        next_cursors: dict[str, int] = {}
        errors: dict[str, str] = {}
        for run in self.store.list_runs():
            source_cursor = make_workflow_source_cursor(run.run_id)
            cursor = cursors.get(source_cursor, 0)
            try:
                rows = self.store.replay_events(run.run_id)
                max_sequence = cursor
                parent_id = make_workflow_run_execution_id(run.run_id)
                for row in rows:
                    if row.sequence <= cursor:
                        continue
                    max_sequence = max(max_sequence, row.sequence)
                    event_execution_id = (
                        make_workflow_child_execution_id(run.run_id, row.job_id)
                        if row.job_id
                        else parent_id
                    )
                    if target is not None and event_execution_id != target.execution_id:
                        continue
                    events.append(
                        AgentEvent.from_workflow_event(
                            row.to_dict(),
                            execution_id=event_execution_id,
                            parent_execution_id=parent_id if row.job_id else None,
                        )
                    )
                next_cursors[source_cursor] = max_sequence
            except Exception as exc:
                errors[source_cursor] = redact_sensitive_text(str(exc))
                next_cursors[source_cursor] = cursor
        events.sort(key=lambda event: (event.source_cursor or "", event.source_sequence or 0, event.event_id or ""))
        return AgentEventBatch(events=tuple(events), next_cursors=next_cursors, errors=errors)

    def read_result(self, execution_id: str, *, include_preview: bool = False) -> AgentResultRecord:
        target = self._find_target(execution_id)
        if target is None:
            return AgentResultRecord(execution_id=execution_id, status="unknown", error="not_found")
        run, job, record = target
        ref = record.artifact_refs[0].ref if record.artifact_refs else None
        payload = {}
        if include_preview and ref:
            try:
                artifact_path = self._safe_artifact_path(run, ref)
                data = json.loads(artifact_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    source_payload = data.get("payload") if isinstance(data.get("payload"), dict) else data
                    payload = {"preview": self._preview(source_payload)}
            except (FileNotFoundError, OSError, ValueError, json.JSONDecodeError):
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
        requested = getattr(request, "action", "")
        if not record.capabilities.supports_action(requested):
            return ControlResult(
                ok=False,
                code="unsupported_capability",
                execution_id=execution_id,
                scope=record.record_kind,
                status=record.status,
                data={"requestedAction": requested, "capabilities": sorted(record.capabilities.actions)},
            )
        if record.record_kind != "workflow_run" or self.controller is None:
            return ControlResult(
                ok=False,
                code="unsupported_capability",
                execution_id=execution_id,
                scope=record.record_kind,
                status=record.status,
                data={"requestedAction": requested, "capabilities": sorted(record.capabilities.actions)},
            )
        try:
            reason = getattr(request, "reason", "") or ""
            if requested == "stop":
                updated = self.controller.stop(record.run_id, reason=reason)
            elif requested == "cancel":
                updated = self.controller.cancel(record.run_id, reason=reason)
            else:
                return ControlResult(
                    ok=False,
                    code="unsupported_capability",
                    execution_id=execution_id,
                    scope=record.record_kind,
                    status=record.status,
                )
            return ControlResult(ok=True, code="ok", execution_id=execution_id, scope=record.record_kind, status=updated.status)
        except Exception as exc:
            return ControlResult(
                ok=False,
                code="control_error",
                execution_id=execution_id,
                scope=record.record_kind,
                status=record.status,
                message=redact_sensitive_text(str(exc)),
            )

    def _records_for_run(self, run: WorkflowRun) -> tuple[AgentRecord, list[AgentRecord]]:
        summary = summarize_workflow_jobs(run.jobs)
        outcome = project_workflow_execution_outcome(run.status, summary)
        run_metadata = _safe_metadata(
            run.metadata,
            (
                "workspacePath",
                "permissionProfile",
                "permissionPolicyVersion",
                "resumeFromRunId",
                "plannerMode",
                "workflowTaskType",
                "workflowDraftRef",
                "workflowIssues",
            ),
        )
        run_metadata["childSummary"] = summary
        if outcome is not None:
            run_metadata["executionOutcome"] = outcome
        run_ref = run.result_ref or "final-result.json"
        run_artifacts = _artifact_ref(run_ref, artifact_type="workflow_result")
        run_actions = set(_READ_ACTIONS)
        if run.status in {"running", "interrupted"}:
            run_actions.add("stop")
        if run.status in {"draft", "awaiting_approval", "running", "interrupted"}:
            run_actions.add("cancel")
        run_record = AgentRecord(
            execution_id=make_workflow_run_execution_id(run.run_id),
            engine="workflow",
            record_kind="workflow_run",
            status=self._run_status(run.status, outcome),
            source_status=run.status,
            parent_execution_id=None,
            run_id=run.run_id,
            workspace=run_metadata.get("workspacePath"),
            permission_profile=run.permission_profile,
            permission_policy_version=run.permission_policy_version,
            capability_snapshot=dict(run.metadata.get("capabilitySnapshot") or {}) if isinstance(run.metadata, dict) else {},
            artifact_refs=run_artifacts,
            capabilities=AgentCapabilities(actions=frozenset(run_actions)),
            error=redact_sensitive_text(run.error) if run.error else None,
            metadata=run_metadata,
        )
        children = [self._child_record(run, job) for job in run.jobs]
        return run_record, children

    def _child_record(self, run: WorkflowRun, job: WorkflowJob) -> AgentRecord:
        metadata = job.metadata if isinstance(job.metadata, dict) else {}
        transcript_ref = metadata.get("transcriptRef")
        if not transcript_ref and job.result_ref:
            try:
                transcript_ref = self.store.read_agent_result(run, job).transcript_ref
            except (FileNotFoundError, ValueError):
                transcript_ref = None
        capability_snapshot = self._read_capability_snapshot(run, transcript_ref)
        safe_metadata = _safe_metadata(
            metadata,
            (
                "label",
                "callIndex",
                "cachedFromRunId",
                "cachedFromJobId",
                "workspacePath",
                "permissionProfile",
                "permissionPolicyVersion",
                "schemaValidation",
            ),
        )
        result_ref = job.result_ref
        return AgentRecord(
            execution_id=make_workflow_child_execution_id(run.run_id, job.job_id),
            engine="workflow",
            record_kind="workflow_child",
            status=_JOB_STATUS_MAP.get(job.status, "unknown"),
            source_status=job.status,
            parent_execution_id=make_workflow_run_execution_id(run.run_id),
            run_id=run.run_id,
            job_id=job.job_id,
            cached=job.status == "cached",
            workspace=metadata.get("workspacePath") or (run.metadata or {}).get("workspacePath"),
            permission_profile=metadata.get("permissionProfile") or run.permission_profile,
            permission_policy_version=metadata.get("permissionPolicyVersion") or run.permission_policy_version,
            capability_snapshot=capability_snapshot,
            artifact_refs=_artifact_ref(result_ref, artifact_type="agent_result"),
            transcript_ref=transcript_ref,
            capabilities=AgentCapabilities(actions=_READ_ACTIONS),
            error=redact_sensitive_text(job.error) if job.error else None,
            metadata=safe_metadata,
        )

    def _find_target(self, execution_id: str):
        for run in self.store.list_runs():
            run_record, child_records = self._records_for_run(run)
            if run_record.execution_id == execution_id:
                return run, None, run_record
            for job, record in zip(run.jobs, child_records):
                if record.execution_id == execution_id:
                    return run, job, record
        return None

    def _read_capability_snapshot(self, run: WorkflowRun, transcript_ref: str | None) -> dict:
        if not transcript_ref:
            return {}
        try:
            events = self.store.read_agent_transcript_events(run, transcript_ref)
        except (FileNotFoundError, ValueError, OSError):
            return {}
        snapshot = {}
        for event in events:
            if event.get("type") == "capability_snapshot" and isinstance(event.get("capabilities"), dict):
                snapshot = event["capabilities"]
        return sanitize(copy.deepcopy(snapshot))

    @staticmethod
    def _run_status(raw_status: str, outcome: str | None) -> str:
        if raw_status in {"succeeded", "completed"} and outcome == "partial":
            return "partial"
        return _RUN_STATUS_MAP.get(raw_status, "unknown")

    @staticmethod
    def _preview(value, limit: int = 240) -> str:
        if isinstance(value, str):
            text = value
        else:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        return " ".join(text.split())[:limit]

    def _safe_artifact_path(self, run: WorkflowRun, ref: str) -> Path:
        ref_path = Path(str(ref))
        if ref_path.is_absolute() or ".." in ref_path.parts:
            raise ValueError("artifact ref escapes workflow directory")
        root = Path(run.artifact_dir).resolve()
        path = (root / ref_path).resolve()
        if path != root and root not in path.parents:
            raise ValueError("artifact ref escapes workflow directory")
        return path
