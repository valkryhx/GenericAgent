from __future__ import annotations

from pathlib import Path

from workflow_models import WorkflowEvent, WorkflowRun
from workflow_store import WorkflowStore
from workflow_verification import normalize_verification_contract


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
        workspace_path: str | None = None,
        run_workspace: bool = True,
    ) -> WorkflowRun:
        draft = planner.plan(task_text, context or {})
        if workspace_path:
            from workflow_planner import normalize_plan_workspace_paths, render_workflow_plan, validate_workflow_plan
            draft.plan = normalize_plan_workspace_paths(draft.plan, workspace_path)
            planner_validation = getattr(draft, "validation", {}) or {}
            if bool(planner_validation.get("ok")):
                draft.validation = validate_workflow_plan(draft.plan)
            if draft.validation.get("ok"):
                draft.script = render_workflow_plan(draft.plan)
            else:
                draft.script = ""
        validation = getattr(draft, "validation", {}) or {}
        draft_context = getattr(draft, "context", {}) or {}
        classification = getattr(draft, "classification", {}) or {}
        planner_mode = str(draft_context.get("plannerMode") or validation.get("mode") or "unknown")
        task_type = str(classification.get("taskType") or getattr(draft, "plan", {}).get("taskType") or "unknown")
        is_valid = bool(validation.get("ok"))
        script = getattr(draft, "script", "") if is_valid else ""
        if is_valid and script:
            from workflow_planner import validate_rendered_workflow_script

            preflight = validate_rendered_workflow_script(script)
            if not preflight.get("ok"):
                validation = dict(validation)
                validation["ok"] = False
                validation["issues"] = list(validation.get("issues") or [])
                validation["issues"].append({
                    "code": "invalid_workflow_script",
                    "message": str(preflight.get("error") or "workflow script syntax check failed"),
                })
                draft.validation = validation
                is_valid = False
                script = ""
        draft_plan = getattr(draft, "plan", {}) or {}
        run_verification_contract = normalize_verification_contract(draft_plan)
        acceptance_contract = draft_plan.get("acceptance") if isinstance(draft_plan, dict) else None
        run = WorkflowRun(
            session_id=session_id,
            script=script or "",
            metadata={
                "plannerMode": planner_mode,
                "workflowTaskType": task_type,
                "verificationContract": run_verification_contract,
            },
        )
        # Two concurrent runs used to share one workspace directory, so a second
        # run could overwrite or delete the first run's deliverables. Each run now
        # owns ``<base>/workflow-runs/<runId>/``; the base stays in run metadata so
        # handoff refs remain resolvable from the GA workspace root, and the
        # planner keeps emitting workspace-relative paths either way.
        if run_workspace and workspace_path:
            from workflow_workspace import create_run_workspace, workspace_metadata

            try:
                run_workspace_dir = create_run_workspace(workspace_path, run.run_id)
            except (OSError, RuntimeError, ValueError) as exc:
                run.metadata.setdefault("workflowIssues", []).append({
                    "code": "run_workspace_unavailable",
                    "message": f"per-run workspace could not be created; falling back to the shared workspace: {exc}",
                })
            else:
                run.metadata.update(workspace_metadata(run_workspace_dir))
                run.metadata["workspaceBasePath"] = str(Path(workspace_path).expanduser().resolve())
        # A planner that could not produce a model-authored plan silently fell
        # back to the deterministic template. That plan still runs, but it is a
        # partial-quality delivery, so the run must terminate as ``degraded``
        # instead of claiming a clean success the caller cannot distinguish
        # from a real planned run.
        if planner_mode == "fallback_deterministic":
            fallback_reason = str(draft_context.get("fallbackReason") or validation.get("fallbackReason") or "").strip()
            run.metadata["plannerDegraded"] = True
            run.metadata["plannerFallbackReason"] = fallback_reason or None
            run.metadata.setdefault("workflowIssues", []).append({
                "code": "planner_fallback_deterministic",
                "message": "planner fell back to the deterministic template; plan was not model-authored",
                "reason": fallback_reason or None,
            })
        if isinstance(acceptance_contract, dict):
            acceptance_contract = dict(acceptance_contract)
            # The runtime decides whether an empty unittest gate is "not
            # applicable" or a hard failure; that hinges on whether this plan
            # ever declared test work, so carry the declaration with the contract.
            from workflow_planner import plan_declares_tests

            acceptance_contract.setdefault("testsDeclared", plan_declares_tests(draft_plan))
            run.metadata["acceptanceContract"] = acceptance_contract
        for key in ("mode", "riskLevel", "evalContract", "orchestration", "executionContract"):
            if key in draft_plan:
                run.metadata[key] = draft_plan[key]
        orchestration = draft_plan.get("orchestration") if isinstance(draft_plan, dict) else None
        approval_required = bool(isinstance(orchestration, dict) and orchestration.get("approvalRequired"))
        run.metadata["approvalGate"] = {
            "required": approval_required,
            "reason": "explicit_workflow_approval_gate" if approval_required else None,
        }
        run = self.store.create_run(run)
        draft_ref = self.store.write_workflow_draft(run, draft)
        run.metadata["workflowDraftRef"] = draft_ref
        contract_refs = self.store.write_workflow_contract_artifacts(run, draft)
        run.metadata["workflowContractRefs"] = contract_refs
        self._save_and_publish(run)
        self._append(
            run,
            "workflow_planned",
            payload={
                "workflowDraftRef": draft_ref,
                "plannerMode": planner_mode,
                "taskType": task_type,
                "validationOk": is_valid,
                "workflowContractRefs": contract_refs,
            },
        )
        if is_valid and auto_approve and not approval_required:
            run.status = "running"
            self._save_and_publish(run)
            self._append(run, "workflow_started")
        elif is_valid:
            run.status = "awaiting_approval"
            self._save_and_publish(run)
            self._append(
                run,
                "workflow_approval_requested",
                payload={"reason": run.metadata["approvalGate"].get("reason") or "caller_requested_approval"},
            )
        else:
            run.status = "failed"
            run.error = "workflow_plan_rejected"
            self._save_and_publish(run)
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
