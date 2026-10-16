from __future__ import annotations

from dataclasses import dataclass, field
from urllib.parse import quote

from sensitive_redaction import sanitize


def _encoded(value) -> str:
    return quote(str(value or ""), safe="")


def make_process_execution_id(run_id: str | None, agent_path: str | None) -> str:
    return f"process-agent:{_encoded(run_id)}:{_encoded(agent_path)}"


def make_workflow_run_execution_id(run_id: str) -> str:
    return f"workflow-run:{_encoded(run_id)}"


def make_workflow_child_execution_id(run_id: str, job_id: str) -> str:
    return f"workflow-child:{_encoded(run_id)}:{_encoded(job_id)}"


def make_workflow_source_cursor(run_id: str) -> str:
    return f"workflow:{_encoded(run_id)}"


@dataclass(frozen=True)
class AgentStatus:
    turn_status: str | None = None
    process_status: str | None = None

    def to_dict(self) -> dict:
        return {
            "turnStatus": self.turn_status,
            "processStatus": self.process_status,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "AgentStatus":
        data = data or {}
        return cls(
            turn_status=data.get("turnStatus", data.get("turn_status")),
            process_status=data.get("processStatus", data.get("process_status")),
        )


@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    artifact_type: str = "final_output"
    ref: str | None = None
    engine: str | None = None

    def to_dict(self) -> dict:
        return {
            "artifactId": self.artifact_id,
            "artifactType": self.artifact_type,
            "ref": self.ref,
            "engine": self.engine,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "ArtifactRef | None":
        if not data:
            return None
        return cls(
            artifact_id=str(data.get("artifactId") or data.get("artifact_id") or ""),
            artifact_type=str(data.get("artifactType") or data.get("artifact_type") or "final_output"),
            ref=data.get("ref"),
            engine=data.get("engine"),
        )


@dataclass(frozen=True)
class AgentCapabilities:
    actions: frozenset[str] = frozenset()
    features: frozenset[str] = frozenset()

    def supports(self, name: str) -> bool:
        return name in self.actions or name in self.features

    def supports_action(self, name: str) -> bool:
        return name in self.actions

    def to_dict(self) -> dict:
        return {
            "actions": sorted(self.actions),
            "features": sorted(self.features),
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "AgentCapabilities":
        data = data or {}
        return cls(
            actions=frozenset(str(item) for item in data.get("actions") or []),
            features=frozenset(str(item) for item in data.get("features") or []),
        )


@dataclass(frozen=True)
class AgentRecord:
    execution_id: str
    engine: str
    record_kind: str
    status: str
    source_status: str | None = None
    agent_path: str | None = None
    parent_execution_id: str | None = None
    run_id: str | None = None
    job_id: str | None = None
    logical_key: str | None = None
    attempt_id: str | None = None
    attempt_index: int | None = None
    attempt_count: int | None = None
    cached: bool = False
    task_name: str | None = None
    turn_status: str | None = None
    process_status: str | None = None
    workspace: str | None = None
    permission_profile: str | None = None
    permission_policy_version: str | None = None
    capability_snapshot: dict = field(default_factory=dict)
    artifact_refs: tuple[ArtifactRef, ...] = ()
    transcript_ref: str | None = None
    capabilities: AgentCapabilities = field(default_factory=AgentCapabilities)
    created_at: str | None = None
    updated_at: str | None = None
    error: str | None = None
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "executionId": self.execution_id,
            "engine": self.engine,
            "recordKind": self.record_kind,
            "status": self.status,
            "sourceStatus": self.source_status,
            "agentPath": self.agent_path,
            "parentExecutionId": self.parent_execution_id,
            "runId": self.run_id,
            "jobId": self.job_id,
            "logicalKey": self.logical_key,
            "attemptId": self.attempt_id,
            "attemptIndex": self.attempt_index,
            "attemptCount": self.attempt_count,
            "cached": self.cached,
            "taskName": self.task_name,
            "turnStatus": self.turn_status,
            "processStatus": self.process_status,
            "workspace": self.workspace,
            "permissionProfile": self.permission_profile,
            "permissionPolicyVersion": self.permission_policy_version,
            "capabilitySnapshot": sanitize(self.capability_snapshot),
            "artifactRefs": [ref.to_dict() for ref in self.artifact_refs],
            "transcriptRef": self.transcript_ref,
            "capabilities": self.capabilities.to_dict(),
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
            "error": self.error,
            "metadata": sanitize(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AgentRecord":
        def value(camel: str, snake: str, default=None):
            return data.get(camel, data.get(snake, default))

        attempt_index = value("attemptIndex", "attempt_index")
        attempt_count = value("attemptCount", "attempt_count")
        return cls(
            execution_id=str(value("executionId", "execution_id", "")),
            engine=str(value("engine", "engine", "")),
            record_kind=str(value("recordKind", "record_kind", "")),
            status=str(value("status", "status", "unknown")),
            source_status=value("sourceStatus", "source_status"),
            agent_path=value("agentPath", "agent_path"),
            parent_execution_id=value("parentExecutionId", "parent_execution_id"),
            run_id=value("runId", "run_id"),
            job_id=value("jobId", "job_id"),
            logical_key=value("logicalKey", "logical_key"),
            attempt_id=value("attemptId", "attempt_id"),
            attempt_index=int(attempt_index) if attempt_index is not None else None,
            attempt_count=int(attempt_count) if attempt_count is not None else None,
            cached=bool(value("cached", "cached", False)),
            task_name=value("taskName", "task_name"),
            turn_status=value("turnStatus", "turn_status"),
            process_status=value("processStatus", "process_status"),
            workspace=value("workspace", "workspace"),
            permission_profile=value("permissionProfile", "permission_profile"),
            permission_policy_version=value("permissionPolicyVersion", "permission_policy_version"),
            capability_snapshot=dict(value("capabilitySnapshot", "capability_snapshot", {}) or {}),
            artifact_refs=tuple(ArtifactRef.from_dict(item) for item in value("artifactRefs", "artifact_refs", []) if item),
            transcript_ref=value("transcriptRef", "transcript_ref"),
            capabilities=AgentCapabilities.from_dict(value("capabilities", "capabilities", {})),
            created_at=value("createdAt", "created_at"),
            updated_at=value("updatedAt", "updated_at"),
            error=value("error", "error"),
            metadata=dict(value("metadata", "metadata", {}) or {}),
        )


@dataclass(frozen=True)
class AgentResultRecord:
    execution_id: str
    status: str
    payload: dict = field(default_factory=dict)
    final_text_ref: ArtifactRef | None = None
    transcript_ref: str | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "executionId": self.execution_id,
            "status": self.status,
            "payload": sanitize(self.payload),
            "finalTextRef": self.final_text_ref.to_dict() if self.final_text_ref else None,
            "transcriptRef": self.transcript_ref,
            "error": self.error,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AgentResultRecord":
        return cls(
            execution_id=data.get("executionId", data.get("execution_id", "")),
            status=data.get("status", "unknown"),
            payload=dict(data.get("payload") or {}),
            final_text_ref=ArtifactRef.from_dict(data.get("finalTextRef", data.get("final_text_ref"))),
            transcript_ref=data.get("transcriptRef", data.get("transcript_ref")),
            error=data.get("error"),
        )


@dataclass(frozen=True)
class AgentEvent:
    sequence: int
    event_type: str
    agent_path: str
    run_id: str | None = None
    status: AgentStatus = field(default_factory=AgentStatus)
    payload: dict = field(default_factory=dict)
    artifact_ref: ArtifactRef | None = None
    event_id: str | None = None
    engine: str | None = None
    execution_id: str | None = None
    record_kind: str | None = None
    parent_execution_id: str | None = None
    job_id: str | None = None
    logical_key: str | None = None
    attempt_id: str | None = None
    attempt_index: int | None = None
    source_sequence: int | None = None
    source_cursor: str | None = None
    occurred_at: str | None = None

    @classmethod
    def from_subagent_event(cls, raw, *, execution_id: str | None = None):
        status = raw.get("status") or {}
        payload = sanitize(raw.get("payload") or {})
        event_seq = int(raw.get("event_seq") or 0)
        agent_path = str(raw.get("agent_path") or "")
        run_id = raw.get("run_id")
        artifact_id = payload.get("final_output_ref") or raw.get("final_output_ref")
        return cls(
            sequence=event_seq,
            event_type=str(raw.get("type") or ""),
            agent_path=agent_path,
            run_id=run_id,
            status=AgentStatus(
                turn_status=status.get("turn_status"),
                process_status=status.get("process_status"),
            ),
            payload=dict(payload),
            artifact_ref=ArtifactRef(artifact_id, engine="process") if artifact_id else None,
            event_id=f"process:{raw.get('event_id') or f'evt_{event_seq}'}",
            engine="process",
            execution_id=execution_id or make_process_execution_id(run_id, agent_path),
            record_kind="process_agent",
            job_id=None,
            source_sequence=event_seq,
            source_cursor="process",
            occurred_at=raw.get("created_at") or raw.get("occurred_at"),
        )

    @classmethod
    def from_workflow_event(
        cls,
        raw: dict,
        *,
        execution_id: str,
        parent_execution_id: str | None = None,
        logical_key: str | None = None,
        attempt_id: str | None = None,
        attempt_index: int | None = None,
    ):
        run_id = str(raw.get("runId") or raw.get("run_id") or "")
        job_id = raw.get("jobId") or raw.get("job_id")
        sequence = int(raw.get("sequence") or 0)
        event_id = raw.get("eventId") or raw.get("event_id")
        if not event_id:
            event_id = f"workflow:{_encoded(run_id)}:{sequence}"
            if sequence <= 0 and attempt_id:
                event_id = f"{event_id}:{_encoded(attempt_id)}"
        payload = sanitize(raw.get("payload") or {})
        return cls(
            sequence=sequence,
            event_type=str(raw.get("type") or raw.get("eventType") or raw.get("event_type") or ""),
            agent_path="",
            run_id=run_id,
            status=AgentStatus(),
            payload=dict(payload),
            artifact_ref=None,
            event_id=event_id,
            engine="workflow",
            execution_id=execution_id,
            record_kind="workflow_child" if job_id else "workflow_run",
            parent_execution_id=parent_execution_id,
            job_id=job_id,
            logical_key=logical_key,
            attempt_id=attempt_id,
            attempt_index=attempt_index,
            source_sequence=sequence,
            source_cursor=make_workflow_source_cursor(run_id),
            occurred_at=raw.get("occurredAt") or raw.get("occurred_at"),
        )

    def to_dict(self) -> dict:
        return {
            "eventId": self.event_id,
            "engine": self.engine,
            "executionId": self.execution_id,
            "recordKind": self.record_kind,
            "parentExecutionId": self.parent_execution_id,
            "jobId": self.job_id,
            "logicalKey": self.logical_key,
            "attemptId": self.attempt_id,
            "attemptIndex": self.attempt_index,
            "sequence": self.sequence,
            "sourceSequence": self.source_sequence,
            "sourceCursor": self.source_cursor,
            "type": self.event_type,
            "agentPath": self.agent_path,
            "runId": self.run_id,
            "status": self.status.to_dict(),
            "payload": sanitize(self.payload),
            "artifactRef": self.artifact_ref.to_dict() if self.artifact_ref else None,
            "occurredAt": self.occurred_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AgentEvent":
        status = data.get("status") or {}
        return cls(
            sequence=int(data.get("sequence") or data.get("sourceSequence") or data.get("source_sequence") or 0),
            event_type=str(data.get("type") or data.get("eventType") or data.get("event_type") or ""),
            agent_path=str(data.get("agentPath") or data.get("agent_path") or ""),
            run_id=data.get("runId", data.get("run_id")),
            status=AgentStatus.from_dict(status),
            payload=dict(data.get("payload") or {}),
            artifact_ref=ArtifactRef.from_dict(data.get("artifactRef", data.get("artifact_ref"))),
            event_id=data.get("eventId", data.get("event_id")),
            engine=data.get("engine"),
            execution_id=data.get("executionId", data.get("execution_id")),
            record_kind=data.get("recordKind", data.get("record_kind")),
            parent_execution_id=data.get("parentExecutionId", data.get("parent_execution_id")),
            job_id=data.get("jobId", data.get("job_id")),
            logical_key=data.get("logicalKey", data.get("logical_key")),
            attempt_id=data.get("attemptId", data.get("attempt_id")),
            attempt_index=data.get("attemptIndex", data.get("attempt_index")),
            source_sequence=data.get("sourceSequence", data.get("source_sequence")),
            source_cursor=data.get("sourceCursor", data.get("source_cursor")),
            occurred_at=data.get("occurredAt", data.get("occurred_at")),
        )


@dataclass(frozen=True)
class AgentEventBatch:
    events: tuple[AgentEvent, ...] = ()
    next_cursors: dict[str, int] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "events": [event.to_dict() for event in self.events],
            "nextCursors": {str(key): int(value) for key, value in self.next_cursors.items()},
            "errors": dict(self.errors),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AgentEventBatch":
        return cls(
            events=tuple(AgentEvent.from_dict(item) for item in data.get("events") or []),
            next_cursors={str(key): int(value) for key, value in (data.get("nextCursors") or data.get("next_cursors") or {}).items()},
            errors={str(key): str(value) for key, value in (data.get("errors") or {}).items()},
        )
