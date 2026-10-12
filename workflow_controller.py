from __future__ import annotations

from workflow_models import WorkflowEvent, WorkflowRun
from workflow_store import WorkflowStore


class WorkflowController:
    def __init__(self, store: WorkflowStore | None = None):
        self.store = store or WorkflowStore()

    def create_draft(self, *, session_id: str, script: str) -> WorkflowRun:
        return self.store.create_run(WorkflowRun(session_id=session_id, script=script))


    def _save_and_publish(self, run: WorkflowRun) -> WorkflowRun:
        """Persist the run *and* its progress snapshot.

        ``workflow-progress.json`` used to appear only once the runtime started
        executing jobs. Every status transition before that (planned, awaiting
        approval, rejected, approved, denied, cancelled, stopped) therefore had
        no snapshot, and a reader asking for progress got "workflow progress is
        not available" for a run that was perfectly healthy. A planned run now
        publishes a snapshot the moment it exists, so the UI reflects the plan
        instead of reporting a spurious failure.
        """
        self.store.save_run(run)
        self.store.write_workflow_progress(run)
        return run

    def request_approval(self, run_id: str) -> WorkflowRun:
        run = self.store.load_run(run_id)
        self._require_status(run, {"draft"}, "request approval")
        run.status = "awaiting_approval"
        self._save_and_publish(run)
        self._append(run, "workflow_approval_requested")
        return run

    def approve(self, run_id: str) -> WorkflowRun:
        run = self.store.load_run(run_id)
        self._require_status(run, {"awaiting_approval"}, "approve")
        run.status = "running"
        self._save_and_publish(run)
        self._append(run, "workflow_started")
        return run

    def deny(self, run_id: str, *, reason: str = "") -> WorkflowRun:
        run = self.store.load_run(run_id)
        self._require_status(run, {"awaiting_approval"}, "deny")
        run.status = "cancelled"
        run.error = reason or None
        self._save_and_publish(run)
        self._append(run, "workflow_denied", payload={"reason": reason or ""})
        return run

    def cancel(self, run_id: str, *, reason: str = "") -> WorkflowRun:
        run = self.store.load_run(run_id)
        self._require_status(run, {"draft", "awaiting_approval", "running", "interrupted"}, "cancel")
        run.status = "cancelled"
        run.error = reason or None
        self._save_and_publish(run)
        self._append(run, "workflow_cancelled", payload={"reason": reason or ""})
        return run

    def stop(self, run_id: str, *, reason: str = "") -> WorkflowRun:
        run = self.store.load_run(run_id)
        self._require_status(run, {"running", "interrupted"}, "stop")
        run.status = "killed"
        run.error = reason or None
        self._save_and_publish(run)
        self._append(run, "workflow_killed", payload={"reason": reason or ""})
        return run

    def resume(self, run_id: str) -> WorkflowRun:
        return self.store.project_resume_state(run_id)

    def _append(self, run: WorkflowRun, event_type: str, payload: dict | None = None):
        self.store.append_event(
            run,
            WorkflowEvent(
                run_id=run.run_id,
                session_id=run.session_id,
                event_type=event_type,
                sequence=0,
                payload=payload or {},
            ),
        )

    @staticmethod
    def _require_status(run: WorkflowRun, allowed: set[str], action: str):
        if run.status not in allowed:
            raise ValueError(f"cannot {action} workflow {run.run_id} from {run.status}")
