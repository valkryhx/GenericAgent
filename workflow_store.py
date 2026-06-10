from __future__ import annotations

import json
import shutil
from pathlib import Path

from sensitive_redaction import sanitize
from workflow_models import AgentResult, WorkflowEvent, WorkflowJob, WorkflowRun


PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_ROOT = PROJECT_ROOT / "temp" / "sessions"


class WorkflowStore:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root is not None else DEFAULT_ROOT

    def create_run(self, run: WorkflowRun) -> WorkflowRun:
        artifact_dir = self._artifact_dir(run.session_id, run.run_id)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        run.artifact_dir = str(artifact_dir)
        self._write_json(artifact_dir / "run.json", sanitize(run.to_dict()))
        self._write_json(artifact_dir / "state.json", sanitize(run.to_dict()))
        (artifact_dir / "script.js").write_text(run.script or "", encoding="utf-8")
        (artifact_dir / "journal.jsonl").touch(exist_ok=True)
        final_result = artifact_dir / "final-result.json"
        if not final_result.exists():
            self._write_json(final_result, {})
        return run

    def save_run(self, run: WorkflowRun) -> WorkflowRun:
        artifact_dir = self._run_dir(run)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        run.artifact_dir = str(artifact_dir)
        self._preserve_external_kill(run, artifact_dir)
        self._write_json(artifact_dir / "run.json", sanitize(run.to_dict()))
        self._write_json(artifact_dir / "state.json", sanitize(run.to_dict()))
        (artifact_dir / "script.js").write_text(run.script or "", encoding="utf-8")
        return run

    def load_run(self, run_id: str) -> WorkflowRun:
        artifact_dir = self._find_run_dir(run_id)
        data_path = artifact_dir / "state.json"
        if not data_path.exists():
            data_path = artifact_dir / "run.json"
        data = json.loads(data_path.read_text(encoding="utf-8"))
        run = WorkflowRun.from_dict(data)
        run.artifact_dir = str(artifact_dir)
        script_path = artifact_dir / "script.js"
        if script_path.exists():
            run.script = script_path.read_text(encoding="utf-8")
        return run

    def append_event(self, run: WorkflowRun | str, event: WorkflowEvent) -> WorkflowEvent:
        artifact_dir = self._run_dir(run) if isinstance(run, WorkflowRun) else self._find_run_dir(run)
        with (artifact_dir / "journal.jsonl").open("a", encoding="utf-8", errors="replace") as fh:
            fh.write(json.dumps(sanitize(event.to_dict()), ensure_ascii=False, separators=(",", ":")) + "\n")
        return event

    def append_permission_event(self, run: WorkflowRun, raw_event: dict) -> WorkflowEvent:
        raw_run_id = raw_event.get("runId") or raw_event.get("run_id")
        if raw_run_id and raw_run_id != run.run_id:
            raise ValueError(f"permission event runId mismatch: {raw_run_id} != {run.run_id}")
        event_type = str(raw_event.get("type") or raw_event.get("eventType") or raw_event.get("event_type") or "")
        if event_type not in {"permission_profile_selected", "tool_allowed", "tool_denied"}:
            raise ValueError(f"unsupported permission event type: {event_type}")
        payload = {
            "toolName": raw_event.get("toolName") or raw_event.get("tool_name"),
            "profile": raw_event.get("profile"),
            "decision": raw_event.get("decision"),
            "reason": raw_event.get("reason"),
            "permission": raw_event.get("permission") or {},
        }
        for key, value in raw_event.items():
            if key not in {"type", "eventType", "event_type", "runId", "run_id", "sessionId", "session_id", "jobId", "job_id"} and key not in payload:
                payload[key] = value
        event = WorkflowEvent(
            run_id=run.run_id,
            session_id=run.session_id,
            job_id=raw_event.get("jobId") or raw_event.get("job_id"),
            event_type=event_type,
            sequence=self._next_sequence(run.run_id),
            payload=payload,
        )
        return self.append_event(run, event)

    def write_agent_result(self, run: WorkflowRun, job: WorkflowJob, result: AgentResult) -> str:
        result_ref = f"agents/{job.job_id}/result.json"
        result_path = self._run_dir(run) / result_ref
        result_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_json(result_path, sanitize(result.to_artifact_dict()))
        job.result_ref = result_ref
        return result_ref

    def read_agent_result(self, run: WorkflowRun | str, job: WorkflowJob | str) -> AgentResult:
        artifact_dir = self._run_dir(run) if isinstance(run, WorkflowRun) else self._find_run_dir(run)
        if isinstance(job, WorkflowJob):
            result_ref = job.result_ref or f"agents/{job.job_id}/result.json"
        else:
            result_ref = f"agents/{job}/result.json"
        result_path = artifact_dir / result_ref
        if not result_path.exists():
            raise FileNotFoundError(str(result_path))
        return AgentResult.from_dict(json.loads(result_path.read_text(encoding="utf-8")))

    def write_agent_transcript(self, run: WorkflowRun, job: WorkflowJob, events_or_messages: list[dict]) -> str:
        transcript_ref = f"agents/{job.job_id}/transcript.jsonl"
        transcript_path = self._run_dir(run) / transcript_ref
        transcript_path.parent.mkdir(parents=True, exist_ok=True)
        with transcript_path.open("w", encoding="utf-8", errors="replace") as fh:
            for event in events_or_messages:
                fh.write(json.dumps(sanitize(event), ensure_ascii=False, separators=(",", ":")) + "\n")
        return transcript_ref

    def copy_agent_transcript(self, source_run: WorkflowRun | str, source_ref: str, target_run: WorkflowRun, target_job: WorkflowJob) -> str | None:
        if not source_ref:
            return None
        source_path = self._run_dir(source_run) / source_ref if isinstance(source_run, WorkflowRun) else self._find_run_dir(source_run) / source_ref
        if not source_path.exists():
            return None
        target_ref = f"agents/{target_job.job_id}/transcript.jsonl"
        target_path = self._run_dir(target_run) / target_ref
        target_path.parent.mkdir(parents=True, exist_ok=True)
        rows = []
        for line in source_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            rows.append(sanitize(json.loads(line)))
        with target_path.open("w", encoding="utf-8", errors="replace") as fh:
            for row in rows:
                fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        return target_ref

    def write_final_result(self, run: WorkflowRun, payload: dict) -> str:
        result_ref = "final-result.json"
        self._write_json(self._run_dir(run) / result_ref, sanitize(payload))
        run.result_ref = result_ref
        return result_ref

    def replay_events(self, run_id: str) -> list[WorkflowEvent]:
        artifact_dir = self._find_run_dir(run_id)
        journal_path = artifact_dir / "journal.jsonl"
        if not journal_path.exists():
            return []
        events: list[WorkflowEvent] = []
        for line in journal_path.read_text(encoding="utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            events.append(WorkflowEvent.from_dict(json.loads(line)))
        return events

    def project_resume_state(self, run_id: str) -> WorkflowRun:
        run = self.load_run(run_id)
        changed = False
        if run.status == "running":
            run.status = "interrupted"
            changed = True
        for job in run.jobs:
            if job.status == "running":
                job.status = "stale"
                changed = True
        running_job_ids = {
            event.job_id
            for event in self.replay_events(run_id)
            if event.event_type in {"job_running", "agent_started"} and event.job_id
        }
        known_job_ids = {job.job_id for job in run.jobs}
        for job_id in sorted(running_job_ids - known_job_ids):
            run.jobs.append(WorkflowJob(job_id=job_id, status="stale"))
            changed = True
        if changed:
            next_sequence = max((event.sequence for event in self.replay_events(run_id)), default=0) + 1
            self.append_event(
                run,
                WorkflowEvent(
                    run_id=run.run_id,
                    session_id=run.session_id,
                    event_type="workflow_interrupted",
                    sequence=next_sequence,
                ),
            )
            self.save_run(run)
        return run

    def _artifact_dir(self, session_id: str, run_id: str) -> Path:
        return self.root / session_id / "workflows" / run_id

    def _run_dir(self, run: WorkflowRun) -> Path:
        if run.artifact_dir:
            return Path(run.artifact_dir)
        return self._artifact_dir(run.session_id, run.run_id)

    def _find_run_dir(self, run_id: str) -> Path:
        matches = list(self.root.glob(f"*/workflows/{run_id}"))
        if not matches:
            raise FileNotFoundError(run_id)
        return matches[0]

    def _next_sequence(self, run_id: str) -> int:
        return max((event.sequence for event in self.replay_events(run_id)), default=0) + 1

    def _preserve_external_kill(self, run: WorkflowRun, artifact_dir: Path) -> None:
        if run.status == "killed":
            return
        state_path = artifact_dir / "state.json"
        if not state_path.exists():
            return
        current = WorkflowRun.from_dict(json.loads(state_path.read_text(encoding="utf-8")))
        if current.status != "killed":
            return
        run.status = "killed"
        run.error = current.error

    @staticmethod
    def _write_json(path: Path, data: dict):
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
