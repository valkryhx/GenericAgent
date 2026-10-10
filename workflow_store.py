from __future__ import annotations

import copy
import hashlib
import json
import shutil
from pathlib import Path

from sensitive_redaction import redact_sensitive_text, sanitize
from subagent_state import (
    atomic_write_json,
    atomic_write_text,
    cross_process_lock,
    read_json_retrying,
    read_text_retrying,
)
from workflow_models import (
    AgentResult,
    WorkflowEvent,
    WorkflowJob,
    WorkflowRun,
    refresh_workflow_execution_metadata,
)


def copy_tool_summary(value) -> dict:
    return copy.deepcopy(value) if isinstance(value, dict) else {}


def copy_token_usage(value) -> dict:
    return copy.deepcopy(value) if isinstance(value, dict) else {}


from workflow_workspace import workspace_writes_with_writer

PROJECT_ROOT = Path(__file__).resolve().parent
DEFAULT_ROOT = PROJECT_ROOT / "temp" / "sessions"


def build_artifact_ownership_index(run: WorkflowRun) -> dict[str, list[str]]:
    """Run-level ``path -> writers`` index derived from diff-time ownership.

    A downstream reader needs to know which job produced a file even when the
    producing stage kept no in-process handoff dict (a child process does not).
    It never re-infers a writer from a tool name: the writer is recorded once,
    at workspace-diff time, and this only unions it across the run.
    """
    index: dict[str, list[str]] = {}
    for job in getattr(run, "jobs", None) or []:
        metadata = job.metadata if isinstance(job.metadata, dict) else {}
        for entry in workspace_writes_with_writer(metadata.get("observedArtifacts")):
            writer = entry.get("writer") or getattr(job, "job_id", "") or ""
            if not writer:
                continue
            bucket = index.setdefault(entry["path"], [])
            if writer not in bucket:
                bucket.append(writer)
    return index


class WorkflowStore:
    def __init__(self, root: str | Path | None = None):
        self.root = Path(root) if root is not None else DEFAULT_ROOT

    def create_run(self, run: WorkflowRun) -> WorkflowRun:
        artifact_dir = self._artifact_dir(run.session_id, run.run_id)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        run.artifact_dir = str(artifact_dir)
        self._write_json(artifact_dir / "run.json", sanitize(run.to_dict()))
        self._write_json(artifact_dir / "state.json", sanitize(run.to_dict()))
        self._write_text(artifact_dir / "script.js", run.script or "")
        (artifact_dir / "journal.jsonl").touch(exist_ok=True)
        final_result = artifact_dir / "final-result.json"
        if not final_result.exists():
            self._write_json(final_result, {})
        return run

    def save_run(self, run: WorkflowRun) -> WorkflowRun:
        artifact_dir = self._run_dir(run)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        run.artifact_dir = str(artifact_dir)
        # The kill guard is a read-modify-write, so it only works while it is the only one running.
        # Without this lock the scheduler's ~20ms `save_run` could read `state.json` before an
        # external kill landed and write `running` back over it, and the workflow ran to its
        # deadline after the user pressed stop. Same defect class as the subagent registry (M5).
        with cross_process_lock(self._run_lock_path(artifact_dir)):
            self._preserve_external_kill(run, artifact_dir)
            self._write_json(artifact_dir / "run.json", sanitize(run.to_dict()))
            self._write_json(artifact_dir / "state.json", sanitize(run.to_dict()))
            self._write_text(artifact_dir / "script.js", run.script or "")
        return run

    def load_run(self, run_id: str) -> WorkflowRun:
        artifact_dir = self._find_run_dir(run_id)
        data_path = artifact_dir / "state.json"
        if not data_path.exists():
            data_path = artifact_dir / "run.json"
        data = read_json_retrying(data_path)
        run = WorkflowRun.from_dict(data)
        run.artifact_dir = str(artifact_dir)
        script_path = artifact_dir / "script.js"
        if script_path.exists():
            run.script = read_text_retrying(script_path)
        return run

    def append_event(self, run: WorkflowRun | str, event: WorkflowEvent) -> WorkflowEvent:
        artifact_dir = self._run_dir(run) if isinstance(run, WorkflowRun) else self._find_run_dir(run)
        journal_path = artifact_dir / "journal.jsonl"
        artifact_dir.mkdir(parents=True, exist_ok=True)
        with cross_process_lock(artifact_dir / ".journal.lock"):
            current_max = 0
            if journal_path.exists():
                for line in journal_path.read_text(encoding="utf-8", errors="replace").splitlines():
                    if not line.strip():
                        continue
                    try:
                        current_max = max(current_max, int(json.loads(line).get("sequence") or 0))
                    except (ValueError, TypeError, json.JSONDecodeError):
                        continue
            if event.sequence <= current_max:
                event.sequence = current_max + 1
            with journal_path.open("a", encoding="utf-8", errors="replace") as fh:
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
            sequence=0,
            payload=payload,
        )
        return self.append_event(run, event)

    def write_agent_result(self, run: WorkflowRun, job: WorkflowJob, result: AgentResult) -> str:
        result_ref = f"agents/{job.job_id}/result.json"
        result_path = self._run_dir(run) / result_ref
        result_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_json(result_path, sanitize(result.to_artifact_dict()))
        job.result_ref = result_ref
        metadata = dict(job.metadata) if isinstance(job.metadata, dict) else {}
        metadata["resultSha256"] = self._file_sha256(result_path)
        metadata.pop("resultIntegrity", None)
        job.metadata = metadata
        return result_ref

    def write_test_gate_result(self, run: WorkflowRun, gate_id: str, result: dict) -> str:
        result_ref = f"test-gates/{gate_id}.json"
        result_path = self._run_dir(run) / result_ref
        result_path.parent.mkdir(parents=True, exist_ok=True)
        self._write_json(result_path, sanitize(result))
        return result_ref

    def write_test_failures(self, run: WorkflowRun, text: str) -> str:
        result_ref = "TEST_FAILURES.txt"
        result_path = self._run_dir(run) / result_ref
        self._write_text(result_path, sanitize(text))
        return result_ref

    @staticmethod
    def write_test_failures_to_workspace(workspace: str | Path, text: str) -> str:
        result_path = Path(workspace) / "TEST_FAILURES.txt"
        atomic_write_text(result_path, sanitize(text))
        return "TEST_FAILURES.txt"

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

    def live_telemetry_path(self, run: WorkflowRun | str, job_id: str) -> Path:
        artifact_dir = self._run_dir(run) if isinstance(run, WorkflowRun) else self._find_run_dir(run)
        return artifact_dir / "agents" / str(job_id) / "live.json"

    def write_job_live_telemetry(self, run: WorkflowRun | str, job_id: str, payload: dict) -> None:
        """Persist one running child's live turn/tool/token counters.

        A child only writes its transcript when the job *ends*, so a
        multi-minute child used to be a frozen row in the UI. This file is a
        UI observation channel only: it never feeds a durable contract check,
        and ``workflow-progress.json`` stays the single durable snapshot.
        """
        if not isinstance(payload, dict):
            return
        path = self.live_telemetry_path(run, job_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        self._write_json(path, sanitize(payload))

    def read_job_live_telemetry(self, run: WorkflowRun | str, job_id: str) -> dict | None:
        path = self.live_telemetry_path(run, job_id)
        if not path.exists():
            return None
        try:
            payload = read_json_retrying(path)
        except (OSError, ValueError):
            return None
        return payload if isinstance(payload, dict) else None

    def copy_agent_transcript(self, source_run: WorkflowRun | str, source_ref: str, target_run: WorkflowRun, target_job: WorkflowJob) -> str | None:
        if not source_ref:
            return None
        source_path = self._run_dir(source_run) / source_ref if isinstance(source_run, WorkflowRun) else self._find_run_dir(source_run) / source_ref
        if not source_path.exists():
            return None
        target_ref = f"agents/{target_job.job_id}/transcript.jsonl"
        target_path = self._run_dir(target_run) / target_ref
        target_path.parent.mkdir(parents=True, exist_ok=True)
        with source_path.open("r", encoding="utf-8", errors="replace") as source_fh:
            with target_path.open("w", encoding="utf-8", errors="replace") as target_fh:
                for line in source_fh:
                    if not line.strip():
                        continue
                    row = sanitize(json.loads(line))
                    target_fh.write(json.dumps(row, ensure_ascii=False, separators=(",", ":")) + "\n")
        return target_ref

    def write_final_result(self, run: WorkflowRun, payload: dict) -> str:
        result_ref = "final-result.json"
        self._write_json(self._run_dir(run) / result_ref, sanitize(payload))
        run.result_ref = result_ref
        return result_ref

    def write_final_audit(self, run: WorkflowRun, payload: dict) -> str:
        """Render final-audit.md for full-contract runs from recorded facts.

        Everything here comes from host-side records (the plan's computed eval
        contract, test gate summaries, acceptance/integration metadata). No
        agent prose is read, so the audit cannot be talked into passing.
        """

        metadata = run.metadata if isinstance(run.metadata, dict) else {}
        eval_contract = metadata.get("evalContract") if isinstance(metadata.get("evalContract"), dict) else {}
        surfaces = [item for item in eval_contract.get("sharedSurfaces") or [] if isinstance(item, dict)]
        gates = [gate for gate in (payload.get("testGates") or metadata.get("testGates") or []) if isinstance(gate, dict)]
        payload = payload if isinstance(payload, dict) else {}
        outcome = str(payload.get("status") or run.status or "unknown")

        if not surfaces:
            audit_status = "insufficient_evidence"
        elif outcome == "succeeded" and str(metadata.get("integrationStatus") or "") == "accepted":
            audit_status = "passed"
        else:
            audit_status = "failed"

        lines = [
            "# Final audit",
            "",
            "## Eval contract",
            f"- level: {eval_contract.get('level') or 'unknown'}",
            f"- outcome: {eval_contract.get('outcome') or 'unknown'}",
            f"- shared surfaces: {len(surfaces)}",
            f"- required checks: {', '.join(str(item) for item in eval_contract.get('requiredChecks') or []) or 'none'}",
            f"- blocking conditions: {', '.join(str(item) for item in eval_contract.get('blockingConditions') or []) or 'none'}",
            "",
            "## Shared surfaces",
        ]
        if surfaces:
            lines.extend(["| surface | producer | consumers | structured |", "| --- | --- | --- | --- |"])
            for surface in surfaces:
                consumers = ", ".join(str(item) for item in surface.get("consumers") or []) or "none"
                lines.append(
                    f"| {surface.get('surface') or 'unknown'} | {surface.get('producer') or 'unknown'} | {consumers} | {bool(surface.get('structured'))} |"
                )
        else:
            lines.append("- none enumerated: the plan declared no producer/consumer handoff")
        plan_validation = str(metadata.get("planValidation") or ("pass" if metadata.get("workflowContractRefs") else "unknown"))
        lines.extend(["", "## Checks applied", f"- plan_validation: {plan_validation}"])
        acceptance_status = str(metadata.get("acceptanceStatus") or "not_recorded")
        lines.append(f"- acceptance: {acceptance_status}")
        if metadata.get("acceptanceFailures"):
            lines.extend(f"  - {item}" for item in metadata["acceptanceFailures"])
        if gates:
            for gate in gates:
                lines.append(
                    f"- test gate {gate.get('gateId') or 'unknown'}: {gate.get('status') or 'unknown'}"
                    f" (pass={gate.get('passCount', 0)}, fail={gate.get('failCount', 0)})"
                )
        else:
            lines.append("- test gates: not_applicable")
        lines.extend(
            [
                "",
                "## Integration",
                f"- integrationStatus: {metadata.get('integrationStatus') or 'unknown'}",
                f"- finalAuditStatus: {audit_status}",
                f"- executionOutcome: {metadata.get('executionOutcome') or outcome}",
                f"- run status: {outcome}",
            ]
        )
        if metadata.get("integrationIssues"):
            lines.append("- integrationIssues:")
            lines.extend(f"  - {item}" for item in metadata["integrationIssues"])
        if run.error:
            lines.append(f"- error: {redact_sensitive_text(str(run.error))}")

        audit_ref = "final-audit.md"
        self._write_text(self._run_dir(run) / audit_ref, sanitize("\n".join(lines) + "\n"))
        metadata = dict(metadata)
        metadata["finalAuditStatus"] = audit_status
        metadata["finalAuditRef"] = audit_ref
        run.metadata = metadata
        return audit_ref

    def write_workflow_draft(self, run: WorkflowRun, draft) -> str:
        draft_ref = "workflow-draft.json"
        payload = draft.to_dict() if hasattr(draft, "to_dict") else copy.deepcopy(draft)
        self._write_json(self._run_dir(run) / draft_ref, sanitize(payload))
        return draft_ref

    def write_workflow_contract_artifacts(self, run: WorkflowRun, draft) -> dict[str, str]:
        """Project the machine plan into concise, human-auditable workflow artifacts."""

        plan = copy.deepcopy(getattr(draft, "plan", None) or {})
        context = copy.deepcopy(getattr(draft, "context", None) or {})
        task_text = str(getattr(draft, "task_text", "") or "")
        phases = plan.get("phases") or []
        constraints = context.get("constraints") or plan.get("constraints") or []
        success_criteria = plan.get("successCriteria") or []
        eval_contract = plan.get("evalContract") or {}
        orchestration = plan.get("orchestration") or {}

        plan_lines = [
            f"# {plan.get('meta', {}).get('name') or 'Workflow plan'}",
            "",
            "## Goal",
            task_text,
            "",
            "## Success criteria",
            *[f"- {item}" for item in success_criteria],
            "",
            "## Current context",
            f"- plannerMode: {context.get('plannerMode') or 'unknown'}",
            f"- taskType: {plan.get('taskType') or 'unknown'}",
            f"- workflowContractVersion: {plan.get('workflowContractVersion') or 'unknown'}",
            "",
            "## Constraints",
            *[f"- {item}" for item in constraints],
            "",
            "## Risk level",
            str(plan.get("riskLevel") or "unknown"),
            "",
            "## Mode",
            str(plan.get("mode") or "workflow"),
            "",
            "## Work packets",
        ]
        for index, phase in enumerate(phases, start=1):
            plan_lines.append(f"### {index}. {phase.get('title') or 'Untitled phase'}")
            for agent in phase.get("agents") or []:
                deps = ", ".join(str(item) for item in agent.get("dependsOn") or []) or "none"
                plan_lines.append(
                    f"- {agent.get('label') or 'agent'} (role={agent.get('role') or 'unspecified'}, owner={agent.get('owner') or 'workflow'}, dependsOn={deps})"
                )
        plan_lines.extend(
            [
                "",
                "## Eval contract",
                f"- level: {eval_contract.get('level') or 'unknown'}",
                f"- outcome: {eval_contract.get('outcome') or 'unknown'}",
                f"- requiredChecks: {', '.join(str(item) for item in eval_contract.get('requiredChecks') or []) or 'none'}",
                f"- blockingConditions: {', '.join(str(item) for item in eval_contract.get('blockingConditions') or []) or 'none'}",
                "",
                "## Verification plan",
                "- Plan validator must pass before execution.",
                "- Runtime acceptance and structured verification must pass before success.",
                "",
                "## Completion criteria",
                "- workflow status, execution outcome, acceptance status, and artifacts agree.",
            ]
        )

        orchestration_lines = [
            "# Orchestration",
            "",
            "## Parent critical path",
            *[f"- {item}" for item in orchestration.get("parentCriticalPath") or []],
            "",
            "## Packets",
            *[
                f"- {agent.get('label') or 'agent'}: owner={agent.get('owner') or 'workflow'}, writeScope={agent.get('writeScope') or []}"
                for phase in phases
                for agent in phase.get("agents") or []
            ],
            "",
            "## Delegation",
            f"- allowed: {bool(orchestration.get('delegationAllowed'))}",
            f"- maxAgents: {orchestration.get('maxAgents') or 0}",
            f"- maxWaves: {orchestration.get('maxWaves') or 0}",
            f"- failurePolicy: {orchestration.get('failurePolicy') or 'continue'}",
            "",
            "## Wait points",
            *[f"- {item}" for item in orchestration.get("waitPoints") or []],
            "",
            "## Fallback",
            "- Plan rejection remains fail-closed; no native delegation is simulated.",
            "",
            "## Verification order",
            "- plan validation -> child results -> host acceptance gates -> final result artifact",
        ]

        artifact_dir = self._run_dir(run)
        plan_ref = "plan.md"
        orchestration_ref = "orchestration.md"
        self._write_text(artifact_dir / plan_ref, sanitize("\n".join(plan_lines) + "\n"))
        self._write_text(artifact_dir / orchestration_ref, sanitize("\n".join(orchestration_lines) + "\n"))
        return {"plan": plan_ref, "orchestration": orchestration_ref}

    def write_workflow_progress(self, run: WorkflowRun) -> str:
        progress_ref = "workflow-progress.json"
        metadata = run.metadata if isinstance(run.metadata, dict) else {}
        progress = {
            "runId": run.run_id,
            "sessionId": run.session_id,
            "status": run.status,
            # Two roots, stated explicitly so no reader has to guess. Per-job
            # ``resultRef``/``transcriptRef`` are run-internal and resolve under
            # ``runArtifactDir``; ``observedArtifacts``/``artifactRefs`` are
            # workspace relative and resolve under ``workspacePath``. Joining a
            # ``resultRef`` onto ``workspacePath`` is what made the durable
            # result look missing to the GA agent.
            "runArtifactDir": str(run.artifact_dir) if run.artifact_dir else None,
            "workspacePath": metadata.get("workspacePath"),
            "workspaceBasePath": metadata.get("workspaceBasePath"),
            "workflowIssues": copy.deepcopy((run.metadata or {}).get("workflowIssues") or []),
            "workflowProgress": [self._build_job_progress(run, job, index) for index, job in enumerate(run.jobs, start=1)],
        }
        for key in ("mode", "riskLevel", "orchestration", "approvalGate"):
            if key in metadata:
                progress[key] = copy.deepcopy(metadata[key])
        if "childSummary" in metadata:
            progress["childSummary"] = copy.deepcopy(metadata["childSummary"])
        if "executionOutcome" in metadata:
            progress["executionOutcome"] = metadata["executionOutcome"]
        if "acceptanceStatus" in metadata:
            progress["acceptanceStatus"] = metadata["acceptanceStatus"]
            progress["acceptanceFailures"] = copy.deepcopy(metadata.get("acceptanceFailures") or [])
        for key in ("integrationStatus", "integrationIssues", "finalAuditStatus"):
            if key in metadata:
                progress[key] = copy.deepcopy(metadata[key])
        if "artifactCollisions" in metadata:
            progress["artifactCollisions"] = copy.deepcopy(metadata["artifactCollisions"])
        ownership = build_artifact_ownership_index(run)
        if ownership:
            progress["artifactOwnership"] = ownership
        self._write_json(self._run_dir(run) / progress_ref, sanitize(progress))
        return progress_ref

    def _build_job_progress(self, run: WorkflowRun, job: WorkflowJob, index: int) -> dict:
        result = None
        if job.result_ref:
            try:
                result = self.read_agent_result(run, job)
            except FileNotFoundError:
                result = None
        transcript_ref = job.metadata.get("transcriptRef") or (result.transcript_ref if result else None)
        transcript_events = self.read_agent_transcript_events(run, transcript_ref) if transcript_ref else []
        tool_calls = self._extract_tool_calls(transcript_events)
        loaded_skills = self._extract_loaded_skills(transcript_events)
        capabilities = self._extract_capability_summary(transcript_events)
        tool_summary = copy_tool_summary(result.tool_summary if result else job.metadata.get("toolSummary") or {})
        allowed_tools = list(tool_summary.get("allowedTools") or self._extract_permission_tools(transcript_events, "tool_allowed"))
        denied_tools = list(tool_summary.get("deniedTools") or self._extract_permission_tools(transcript_events, "tool_denied"))
        skill_load_events = self._extract_skill_load_events(transcript_events)
        payload = result.payload if result else {}

        def _absolute(ref):
            if not ref or not run.artifact_dir:
                return None
            return str(Path(run.artifact_dir) / ref)

        handoff = copy.deepcopy(job.metadata.get("handoff") or {}) if isinstance(job.metadata, dict) else {}
        if not isinstance(handoff, dict):
            handoff = {}
        readable_ref = handoff.get("readableResultRef")
        readable_path = handoff.get("resultPath")
        if readable_ref:
            # ``resultRef`` alone is relative to the run's internal artifact
            # directory, so a reader that only knows the run workspace joins it
            # onto the wrong root and finds nothing. Publish the host's readable
            # copy and keep the internal ref under an explicit name.
            handoff["runInternalResultRef"] = job.result_ref
            handoff["resultRef"] = readable_ref

        progress = {
            "type": "workflow_agent",
            "index": index,
            "agentId": job.job_id,
            "jobId": job.job_id,
            "label": job.metadata.get("label"),
            "phase": job.phase,
            "phaseTitle": job.phase,
            "state": job.status,
            # ``resultRef``/``transcriptRef`` are run-internal refs; the absolute
            # ``*Path`` beside each one is the fully resolved location, so a
            # reader never has to join a ref onto the wrong root and conclude the
            # file is missing.
            "resultRef": readable_ref or job.result_ref,
            "resultPath": readable_path or _absolute(job.result_ref),
            "runInternalResultRef": job.result_ref,
            "transcriptRef": transcript_ref,
            "transcriptPath": _absolute(transcript_ref),
            "lastToolName": tool_calls[-1] if tool_calls else None,
            "lastToolSummary": self._last_tool_summary(transcript_events),
            "toolCalls": tool_calls,
            "skillToolCalls": tool_calls.count("load_skill"),
            "skillLoadEvents": skill_load_events,
            "allowedTools": allowed_tools,
            "deniedTools": denied_tools,
            "loadedSkills": loaded_skills,
            "missingRequiredSkills": [],
            "capability": copy.deepcopy(capabilities),
            "capabilities": capabilities,
            "tokenUsage": copy_token_usage(result.token_usage if result else job.metadata.get("tokenUsage") or {}),
            "promptPreview": self._preview(job.prompt),
            "resultPreview": self._preview(payload.get("summary") if isinstance(payload, dict) and payload.get("summary") is not None else payload),
            "error": job.error,
            "schemaValidation": copy.deepcopy(job.metadata.get("schemaValidation") or {}),
            "handoff": handoff,
            "retryPolicy": copy.deepcopy(job.metadata.get("retryPolicy") or {}),
            # Ground truth of files the child actually wrote, so the UI and the
            # Ink handoff can carry resolvable workspace paths instead of the
            # plan's semantic labels ("synthesis").
            "observedArtifacts": [
                {"path": entry["path"], "writer": entry["writer"]}
                for entry in workspace_writes_with_writer(job.metadata.get("observedArtifacts"))
            ],
        }
        return progress

    def _read_agent_transcript_events(self, run: WorkflowRun, transcript_ref: str | None) -> list[dict]:
        return self.read_agent_transcript_events(run, transcript_ref)

    def read_agent_transcript_events(
        self,
        run: WorkflowRun | str,
        transcript_ref: str | None,
        *,
        max_bytes: int = 64_000,
        max_events: int = 256,
    ) -> list[dict]:
        """Return transcript events without positional bias.

        A transcript grows append-only and can be hundreds of KB once a child
        pastes file contents into tool calls. Reading a fixed head window made
        late evidence (for example an artifact readback after a large write)
        invisible: the runtime then failed a run whose evidence was physically
        present. ``max_bytes`` is therefore a per-line safety bound, not a
        scan window; the file is streamed end to end and ``max_events`` bounds
        the returned list.
        """

        if not transcript_ref:
            return []
        max_bytes = max(0, int(max_bytes))
        max_events = max(0, int(max_events))
        if max_bytes == 0 or max_events == 0:
            return []
        artifact_dir = self._run_dir(run) if isinstance(run, WorkflowRun) else self._find_run_dir(run)
        ref_path = Path(str(transcript_ref))
        if ref_path.is_absolute() or ".." in ref_path.parts:
            raise ValueError("transcript_ref must stay within the workflow artifact directory")
        artifact_root = artifact_dir.resolve()
        transcript_path = (artifact_dir / ref_path).resolve()
        if transcript_path != artifact_root and artifact_root not in transcript_path.parents:
            raise ValueError("transcript_ref must stay within the workflow artifact directory")
        if not transcript_path.exists():
            return []

        events: list[dict] = []
        with transcript_path.open("rb") as fh:
            for raw_line in fh:
                if len(events) >= max_events:
                    break
                if len(raw_line) > max_bytes:
                    # Oversized tool payloads stay useful for evidence via
                    # parsing the JSON head; decode a bounded prefix so an
                    # enormous log line cannot blow up memory.
                    raw_line = raw_line[:max_bytes]
                line = raw_line.decode("utf-8", errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(event, dict):
                    events.append(sanitize(event))
        return events

    @staticmethod
    def _extract_tool_calls(events: list[dict]) -> list[str]:
        return [event.get("toolName") for event in events if event.get("type") == "tool_call" and event.get("toolName")]

    @staticmethod
    def _extract_permission_tools(events: list[dict], event_type: str) -> list[str]:
        return [event.get("toolName") for event in events if event.get("type") == event_type and event.get("toolName")]

    @staticmethod
    def _extract_skill_load_events(events: list[dict]) -> list[dict]:
        skill_events: list[dict] = []
        for event in events:
            if event.get("type") != "tool_result" or event.get("toolName") != "load_skill":
                continue
            data = event.get("data")
            if not isinstance(data, dict):
                continue
            item = {
                "name": data.get("name"),
                "status": data.get("status"),
                "source": data.get("source"),
                "path": data.get("path"),
                "baseDir": data.get("base_dir"),
                "allowedTools": data.get("allowed_tools") or [],
            }
            cleaned = {key: value for key, value in item.items() if value not in (None, "", [])}
            if cleaned:
                skill_events.append(cleaned)
        return skill_events


    @staticmethod
    def _extract_loaded_skills(events: list[dict]) -> list[str]:
        loaded: list[str] = []
        for event in events:
            if event.get("type") != "tool_call" or event.get("toolName") != "load_skill":
                continue
            args = event.get("args") or {}
            skill = args.get("skill") or args.get("name") or args.get("skill_name")
            if isinstance(skill, str) and skill and skill not in loaded:
                loaded.append(skill)
        for event in events:
            if event.get("type") != "tool_result" or event.get("toolName") != "load_skill":
                continue
            data = event.get("data") or {}
            skill = data.get("name") if isinstance(data, dict) else None
            if isinstance(skill, str) and skill and skill not in loaded:
                loaded.append(skill)
        return loaded

    @staticmethod
    def _extract_capability_summary(events: list[dict]) -> dict:
        capabilities = {}
        for event in events:
            if event.get("type") == "capability_snapshot" and isinstance(event.get("capabilities"), dict):
                capabilities = event.get("capabilities") or {}
        mcp_discovery = capabilities.get("mcpDiscovery") if isinstance(capabilities.get("mcpDiscovery"), dict) else {}
        mcp_tool_names = capabilities.get("mcpToolNames") if isinstance(capabilities.get("mcpToolNames"), list) else []
        return {
            "loadSkillAvailable": bool(capabilities.get("loadSkillAvailable")),
            "fileReadAvailable": bool(capabilities.get("fileReadAvailable")),
            "mcpDiscoveryStatus": mcp_discovery.get("status"),
            "mcpDiscoveryInjectedToolCount": mcp_discovery.get("injectedToolCount"),
            "mcpToolCount": len(mcp_tool_names),
        }

    @staticmethod
    def _last_tool_summary(events: list[dict]) -> str | None:
        for event in reversed(events):
            if event.get("type") != "tool_result" or not event.get("toolName"):
                continue
            data = event.get("data")
            if isinstance(data, dict):
                for key in ("path", "file", "name", "status", "error"):
                    if data.get(key):
                        return str(data.get(key))[:160]
        return None

    @staticmethod
    def _preview(value, limit: int = 240) -> str:
        if value is None:
            return ""
        if isinstance(value, str):
            text = value
        else:
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        text = " ".join(text.split())
        return text[:limit]

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

    @staticmethod
    def _file_sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _mark_corrupt_result_artifacts_stale(self, run: WorkflowRun) -> bool:
        changed = False
        for job in run.jobs:
            if job.status not in {"succeeded", "cached"} or not job.result_ref:
                continue
            expected = (job.metadata or {}).get("resultSha256") if isinstance(job.metadata, dict) else None
            if not expected:
                continue
            result_path = self._run_dir(run) / job.result_ref
            actual = self._file_sha256(result_path) if result_path.is_file() else None
            if actual == expected:
                continue
            job.status = "stale"
            job.error = "result artifact integrity check failed; result artifact integrity must be revalidated"
            metadata = dict(job.metadata) if isinstance(job.metadata, dict) else {}
            metadata["resultIntegrity"] = "missing" if actual is None else "checksum_mismatch"
            job.metadata = metadata
            changed = True
        return changed

    def project_resume_state(self, run_id: str) -> WorkflowRun:
        run = self.load_run(run_id)
        changed = self._mark_corrupt_result_artifacts_stale(run)
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
            self.append_event(
                run,
                WorkflowEvent(
                    run_id=run.run_id,
                    session_id=run.session_id,
                    event_type="workflow_interrupted",
                    sequence=0,
                ),
            )
            refresh_workflow_execution_metadata(run)
            self.save_run(run)
            self.write_workflow_progress(run)
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

    def list_runs(self) -> list[WorkflowRun]:
        run_ids = sorted({path.parent.name for path in self.root.glob("*/workflows/*/state.json")})
        return [self.load_run(run_id) for run_id in run_ids]

    def _preserve_external_kill(self, run: WorkflowRun, artifact_dir: Path) -> None:
        """Carry a `killed` status that landed on disk onto the row about to be written.

        Callers must already hold `_run_lock_path`: this reads the row it is about to overwrite,
        so an unserialized copy loses exactly the kill it exists to preserve.
        """
        if run.status == "killed":
            return
        state_path = artifact_dir / "state.json"
        if not state_path.exists():
            return
        current = WorkflowRun.from_dict(read_json_retrying(state_path))
        if current.status != "killed":
            return
        run.status = "killed"
        run.error = current.error

    @staticmethod
    def _run_lock_path(artifact_dir: Path) -> Path:
        # Leading dot so it never matches the `*/workflows/*/state.json` scan in the Ink bridge.
        return artifact_dir / ".run.lock"

    @staticmethod
    def _write_json(path: Path, data: dict):
        # tmp + os.replace, so a concurrent `load_run` sees either the old row or the new one.
        # `write_text` truncates first, which produced 11k+ JSONDecodeErrors in 2 seconds under
        # three writers — and `WorkflowRuntime._safe_load_current_run` turns each one into a
        # silently stale row, i.e. another way to miss a kill.
        atomic_write_json(path, data)

    @staticmethod
    def _write_text(path: Path, text: str):
        atomic_write_text(path, text)
