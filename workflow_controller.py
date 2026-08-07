from __future__ import annotations

from workflow_models import WorkflowEvent, WorkflowRun
from workflow_store import WorkflowStore


class WorkflowController:
    def __init__(self, store: WorkflowStore | None = None):
        self.store = store or WorkflowStore()

    def create_draft(self, *, session_id: str, script: str) -> WorkflowRun:
        return self.store.create_run(WorkflowRun(session_id=session_id, script=script))

    def create_planned_run(
        self,
        *,
        session_id: str,
        task_text: str,
        planner,
        context: dict | None = None,
        auto_approve: bool = True,
    ) -> WorkflowRun:
        draft = planner.plan(task_text, context or {})
        validation = getattr(draft, "validation", {}) or {}
        draft_context = getattr(draft, "context", {}) or {}
        classification = getattr(draft, "classification", {}) or {}
        planner_mode = str(draft_context.get("plannerMode") or validation.get("mode") or "deterministic")
        task_type = str(classification.get("taskType") or getattr(draft, "plan", {}).get("taskType") or "unknown")
        is_valid = bool(validation.get("ok"))
        script = getattr(draft, "script", "") if is_valid else ""
        run = WorkflowRun(
            session_id=session_id,
            script=script or "",
            metadata={
                "plannerMode": planner_mode,
                "workflowTaskType": task_type,
            },
        )
        run = self.store.create_run(run)
        draft_ref = self.store.write_workflow_draft(run, draft)
        run.metadata["workflowDraftRef"] = draft_ref
        self.store.save_run(run)
        self._append(
            run,
            "workflow_planned",
            payload={
                "workflowDraftRef": draft_ref,
                "plannerMode": planner_mode,
                "taskType": task_type,
                "validationOk": is_valid,
            },
        )
        if is_valid and auto_approve:
            run.status = "running"
            self.store.save_run(run)
            self._append(run, "workflow_started")
        elif is_valid:
            run.status = "awaiting_approval"
            self.store.save_run(run)
            self._append(run, "workflow_approval_requested")
        else:
            run.status = "failed"
            run.error = "workflow_plan_rejected"
            self.store.save_run(run)
            self._append(
                run,
                "workflow_plan_rejected",
                payload={
                    "workflowDraftRef": draft_ref,
                    "plannerMode": planner_mode,
                    "taskType": task_type,
                    "issues": validation.get("issues") or [],
                    "mode": validation.get("mode"),
                },
            )
        return run

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
                sequence=0,
                payload=payload or {},
            ),
        )

    @staticmethod
    def _require_status(run: WorkflowRun, allowed: set[str], action: str):
        if run.status not in allowed:
            raise ValueError(f"cannot {action} workflow {run.run_id} from {run.status}")
