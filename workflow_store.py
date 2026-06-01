from __future__ import annotations

import json
from pathlib import Path

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
        self._write_json(artifact_dir / "run.json", run.to_dict())
        self._write_json(artifact_dir / "state.json", run.to_dict())
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
        self._write_json(artifact_dir / "run.json", run.to_dict())
        self._write_json(artifact_dir / "state.json", run.to_dict())
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
            fh.write(json.dumps(event.to_dict(), ensure_ascii=False, separators=(",", ":")) + "\n")
        return event

    def write_agent_result(self, run: WorkflowRun, job: WorkflowJob, result: AgentResult) -> str:
        result_ref = f"agents/{job.job_id}/result.json"
        result_path = self._run_dir(run) / result_ref
        result_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_json(result_path, result.to_dict())
        job.result_ref = result_ref
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
            if event.event_type == "job_running" and event.job_id
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

    @staticmethod
    def _write_json(path: Path, data: dict):
        path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
