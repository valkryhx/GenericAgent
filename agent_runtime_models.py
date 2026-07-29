from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class AgentStatus:
    turn_status: str | None = None
    process_status: str | None = None


@dataclass(frozen=True)
class ArtifactRef:
    artifact_id: str
    artifact_type: str = "final_output"


@dataclass(frozen=True)
class AgentEvent:
    sequence: int
    event_type: str
    agent_path: str
    run_id: str | None = None
    status: AgentStatus = field(default_factory=AgentStatus)
    payload: dict = field(default_factory=dict)
    artifact_ref: ArtifactRef | None = None

    @classmethod
    def from_subagent_event(cls, raw):
        status = raw.get("status") or {}
        payload = raw.get("payload") or {}
        artifact_id = payload.get("final_output_ref") or raw.get("final_output_ref")
        return cls(
            sequence=int(raw.get("event_seq") or 0),
            event_type=str(raw.get("type") or ""),
            agent_path=str(raw.get("agent_path") or ""),
            run_id=raw.get("run_id"),
            status=AgentStatus(
                turn_status=status.get("turn_status"),
                process_status=status.get("process_status"),
            ),
            payload=dict(payload),
            artifact_ref=ArtifactRef(artifact_id) if artifact_id else None,
        )
