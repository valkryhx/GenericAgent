from __future__ import annotations

from workflow_models import WorkflowEvent, WorkflowRun
from workflow_store import WorkflowStore


class WorkflowController:
    def __init__(self, store: WorkflowStore | None = None):
        self.store = store or WorkflowStore()

    def create_draft(self, *, session_id: str, script: str) -> WorkflowRun:
        return self.store.create_run(WorkflowRun(session_id=session_id, script=script))

    def request_approval(self, run_id: str) -> WorkflowRun:
        run = self.store.load_run(run_id)
        self._require_status(run, {"draft"}, "request approval")
        run.status = "awaiting_approval"
        self.store.save_run(run)
        self._append(run, "workflow_approval_requested")
        return run

    def approve(self, run_id: str) -> WorkflowRun:
        run = self.store.load_run(run_id)
        self._require_status(run, {"awaiting_approval"}, "approve")
        run.status = "running"
        self.store.save_run(run)
        self._append(run, "workflow_started")
        return run

    def deny(self, run_id: str, *, reason: str = "") -> WorkflowRun:
        run = self.store.load_run(run_id)
        self._require_status(run, {"awaiting_approval"}, "deny")
        run.status = "cancelled"
        run.error = reason or None
        self.store.save_run(run)
        self._append(run, "workflow_denied", payload={"reason": reason or ""})
        return run

    def cancel(self, run_id: str, *, reason: str = "") -> WorkflowRun:
        run = self.store.load_run(run_id)
        self._require_status(run, {"draft", "awaiting_approval", "running", "interrupted"}, "cancel")
        run.status = "cancelled"
        run.error = reason or None
        self.store.save_run(run)
        self._append(run, "workflow_cancelled", payload={"reason": reason or ""})
        return run

    def stop(self, run_id: str, *, reason: str = "") -> WorkflowRun:
        run = self.store.load_run(run_id)
        self._require_status(run, {"running", "interrupted"}, "stop")
        run.status = "killed"
        run.error = reason or None
        self.store.save_run(run)
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
                sequence=self._next_sequence(run.run_id),
                payload=payload or {},
            ),
        )

    def _next_sequence(self, run_id: str) -> int:
        return max((event.sequence for event in self.store.replay_events(run_id)), default=0) + 1

    @staticmethod
    def _require_status(run: WorkflowRun, allowed: set[str], action: str):
        if run.status not in allowed:
            raise ValueError(f"cannot {action} workflow {run.run_id} from {run.status}")
