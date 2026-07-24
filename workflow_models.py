from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass, field


DEFAULT_PERMISSION_PROFILE = "inherit-current-permissions"
DEFAULT_PERMISSION_POLICY_VERSION = "inherit-current-v1"

RUN_STATUSES = frozenset(
    {
        "draft",
        "awaiting_approval",
        "running",
        "succeeded",
        "failed",
        "cancelled",
        "killed",
        "interrupted",
    }
)

JOB_STATUSES = frozenset(
    {
        "registered",
        "queued",
        "running",
        "succeeded",
        "failed",
        "cancelled",
        "killed",
        "cached",
        "skipped",
        "stale",
    }
)


def new_run_id() -> str:
    return "wf_" + uuid.uuid4().hex


def _require_status(status: str, allowed: frozenset[str], kind: str) -> str:
    value = str(status or "")
    if value not in allowed:
        raise ValueError(f"invalid {kind} status: {value}")
    return value


@dataclass
class WorkflowJob:
    job_id: str
    prompt: str = ""
    status: str = "registered"
    phase: str | None = None
    result_ref: str | None = None
    error: str | None = None
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        self.status = _require_status(self.status, JOB_STATUSES, "workflow job")

    def to_dict(self) -> dict:
        return {
            "jobId": self.job_id,
            "prompt": self.prompt,
            "status": self.status,
            "phase": self.phase,
            "resultRef": self.result_ref,
            "error": self.error,
            "metadata": copy.deepcopy(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "WorkflowJob":
        return cls(
            job_id=str(data.get("jobId") or data.get("job_id") or ""),
            prompt=str(data.get("prompt") or ""),
            status=str(data.get("status") or "registered"),
            phase=data.get("phase"),
            result_ref=data.get("resultRef") or data.get("result_ref"),
            error=data.get("error"),
            metadata=copy.deepcopy(data.get("metadata") or {}),
        )


@dataclass
class WorkflowRun:
    session_id: str
    script: str
    run_id: str = field(default_factory=new_run_id)
    status: str = "draft"
    artifact_dir: str | None = None
    permission_profile: str = DEFAULT_PERMISSION_PROFILE
    permission_policy_version: str = DEFAULT_PERMISSION_POLICY_VERSION
    jobs: list[WorkflowJob] = field(default_factory=list)
    result_ref: str | None = None
    error: str | None = None
    metadata: dict = field(default_factory=dict)

    def __post_init__(self):
        self.status = _require_status(self.status, RUN_STATUSES, "workflow run")

    def to_dict(self) -> dict:
        return {
            "version": 1,
            "runId": self.run_id,
            "sessionId": self.session_id,
            "status": self.status,
            "script": self.script,
            "artifactDir": self.artifact_dir,
            "permissionProfile": self.permission_profile,
            "permissionPolicyVersion": self.permission_policy_version,
            "jobs": [job.to_dict() for job in self.jobs],
            "resultRef": self.result_ref,
            "error": self.error,
            "metadata": copy.deepcopy(self.metadata),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "WorkflowRun":
        return cls(
            run_id=str(data.get("runId") or data.get("run_id") or new_run_id()),
            session_id=str(data.get("sessionId") or data.get("session_id") or ""),
            status=str(data.get("status") or "draft"),
            script=str(data.get("script") or ""),
            artifact_dir=data.get("artifactDir") or data.get("artifact_dir"),
            permission_profile=str(
                data.get("permissionProfile")
                or data.get("permission_profile")
                or DEFAULT_PERMISSION_PROFILE
            ),
            permission_policy_version=str(
                data.get("permissionPolicyVersion")
                or data.get("permission_policy_version")
                or DEFAULT_PERMISSION_POLICY_VERSION
            ),
            jobs=[WorkflowJob.from_dict(item) for item in data.get("jobs") or []],
            result_ref=data.get("resultRef") or data.get("result_ref"),
            error=data.get("error"),
            metadata=copy.deepcopy(data.get("metadata") or {}),
        )


@dataclass
class AgentResult:
    job_id: str
    status: str = "succeeded"
    payload: dict = field(default_factory=dict)
    transcript_ref: str | None = None
    token_usage: dict = field(default_factory=dict)
    tool_summary: dict = field(default_factory=dict)
    transcript_events: list[dict] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "jobId": self.job_id,
            "status": self.status,
            "payload": copy.deepcopy(self.payload),
            "transcriptRef": self.transcript_ref,
            "tokenUsage": copy.deepcopy(self.token_usage),
            "toolSummary": copy.deepcopy(self.tool_summary),
            "transcriptEvents": copy.deepcopy(self.transcript_events),
        }

    def to_artifact_dict(self) -> dict:
        return {
            "jobId": self.job_id,
            "status": self.status,
            "payload": copy.deepcopy(self.payload),
            "transcriptRef": self.transcript_ref,
            "tokenUsage": copy.deepcopy(self.token_usage),
            "toolSummary": copy.deepcopy(self.tool_summary),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "AgentResult":
        return cls(
            job_id=str(data.get("jobId") or data.get("job_id") or ""),
            status=str(data.get("status") or "succeeded"),
            payload=copy.deepcopy(data.get("payload") or {}),
            transcript_ref=data.get("transcriptRef") or data.get("transcript_ref"),
            token_usage=copy.deepcopy(data.get("tokenUsage") or data.get("token_usage") or {}),
            tool_summary=copy.deepcopy(data.get("toolSummary") or data.get("tool_summary") or {}),
            transcript_events=copy.deepcopy(data.get("transcriptEvents") or data.get("transcript_events") or []),
        )


@dataclass
class WorkflowEvent:
    run_id: str
    event_type: str
    sequence: int
    session_id: str | None = None
    job_id: str | None = None
    payload: dict = field(default_factory=dict)

    def __post_init__(self):
        if not self.run_id:
            raise ValueError("workflow event requires run_id")
        if not self.event_type:
            raise ValueError("workflow event requires event_type")
        self.sequence = int(self.sequence)

    def to_dict(self) -> dict:
        return {
            "version": 1,
            "type": self.event_type,
            "runId": self.run_id,
            "sessionId": self.session_id,
            "jobId": self.job_id,
            "sequence": self.sequence,
            "payload": copy.deepcopy(self.payload),
        }

    @classmethod
    def from_dict(cls, data: dict) -> "WorkflowEvent":
        return cls(
            run_id=str(data.get("runId") or data.get("run_id") or ""),
            event_type=str(data.get("type") or data.get("eventType") or data.get("event_type") or ""),
            sequence=int(data.get("sequence") or 0),
            session_id=data.get("sessionId") or data.get("session_id"),
            job_id=data.get("jobId") or data.get("job_id"),
            payload=copy.deepcopy(data.get("payload") or {}),
        )
