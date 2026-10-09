from __future__ import annotations

import copy
import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from sensitive_redaction import redact_sensitive_text, sanitize
from workflow_child_agent import AgentResult, ChildAgentRunner, FakeChildAgentRunner, bounded_structured_summary
from workflow_models import WorkflowEvent, WorkflowJob, WorkflowRun, refresh_workflow_execution_metadata
from workflow_store import WorkflowStore, build_artifact_ownership_index
from workflow_workspace import (
    WorkspacePathError,
    normalize_workspace_relative,
    observed_artifact_paths,
    resolve_workspace_child,
    workspace_writes_with_writer,
)
from subagent_state import atomic_write_json


SCHEMA_VALIDATION_FAILED = "schema_validation_failed"
_CACHE_ARGS_UNSET = object()


DEFAULT_RETRYABLE_ERRORS = (
    "timeout",
    "timed out",
    "transient",
    "rate limit",
    "rate_limit",
    "429",
    "provider_anomaly",
    "mcp_transient",
    SCHEMA_VALIDATION_FAILED,
)


def normalize_retry_policy(policy: dict | None) -> dict:
    raw = policy if isinstance(policy, dict) else {}
    max_attempts = raw.get("maxAttempts", 1)
    try:
        max_attempts = int(max_attempts)
    except (TypeError, ValueError):
        max_attempts = 1
    max_attempts = max(1, min(3, max_attempts))
    backoff_ms = raw.get("backoffMs", 0)
    try:
        backoff_ms = int(backoff_ms)
    except (TypeError, ValueError):
        backoff_ms = 0
    backoff_ms = max(0, min(30_000, backoff_ms))
    errors = raw.get("retryableErrors", DEFAULT_RETRYABLE_ERRORS)
    if not isinstance(errors, list):
        errors = list(DEFAULT_RETRYABLE_ERRORS)
    errors = [str(item).strip().lower() for item in errors if str(item).strip()]
    attempts = raw.get("attempts", 0)
    try:
        attempts = int(attempts)
    except (TypeError, ValueError):
        attempts = 0
    return {
        "maxAttempts": max_attempts,
        "retryableErrors": errors,
        "backoffMs": backoff_ms,
        "attempts": max(0, attempts),
        "lastError": raw.get("lastError"),
        "retryNotBefore": raw.get("retryNotBefore"),
        "repairRole": str(raw.get("repairRole") or "").strip()[:64] or None,
        "repairAttempts": max(0, int(raw.get("repairAttempts") or 0)),
    }


def normalize_workflow_workspace(args) -> str | None:
    """Return the canonical workflow workspace from runtime args.

    ``workspacePath`` is the canonical field; ``workspace`` is retained as a
    compatibility alias.  A supplied workspace must be an existing directory
    so every child job can safely use it as its working directory.
    """
    if not isinstance(args, dict):
        return None

    supplied = [(field, args[field]) for field in ("workspacePath", "workspace") if field in args]
    if not supplied:
        return None

    resolved: list[tuple[str, Path]] = []
    for field, raw in supplied:
        if not isinstance(raw, str) or not raw.strip():
            raise ValueError(f"workflow args {field} must be a non-empty path")
        try:
            path = Path(raw).expanduser().resolve()
        except (OSError, RuntimeError, ValueError) as exc:
            raise ValueError(f"workflow args {field} is not a valid path") from exc
        resolved.append((field, path))

    if len(resolved) == 2:
        first = os.path.normcase(os.path.normpath(str(resolved[0][1])))
        second = os.path.normcase(os.path.normpath(str(resolved[1][1])))
        if first != second:
            raise ValueError("workflow args workspacePath and workspace must resolve to the same directory")

    workspace = resolved[0][1]
    if not workspace.is_dir():
        raise ValueError("workflow workspace must be an existing directory")
    return str(workspace)


@dataclass
class SchedulerConfig:
    max_concurrent: int = 4
    max_total: int = 1000

    def __post_init__(self):
        self.max_concurrent = int(self.max_concurrent)
        self.max_total = int(self.max_total)
        if self.max_concurrent < 1:
            raise ValueError("max_concurrent must be at least 1")
        if self.max_concurrent > 16:
            raise ValueError("max_concurrent must be <= 16")
        if self.max_total < 1:
            raise ValueError("max_total must be at least 1")


def normalize_agent_options(options: dict | None) -> dict:
    if options is None:
        return {}
    if not isinstance(options, dict):
        raise TypeError("agent options must be a plain object")
    return dict(options)


def resolve_job_permission_profile(run_profile: str, options: dict | None) -> str:
    """Pick the effective tool policy for one child agent.

    The plan only *declares* intent; the host still enforces the capability. A
    packet that only judges another packet's work must not be able to author the
    artifact it judges, so declared evidence roles get the non-mutating `verify`
    profile even when the run default is permissive. A run-level read-only
    profile is already strictly stronger, so it is never loosened.
    """

    from workflow_permissions import EVIDENCE_ROLES, READ_ONLY, VERIFY

    base = str(run_profile or "").strip() or "inherit-current-permissions"
    role = str((options or {}).get("role") or "").strip().lower()
    if role in EVIDENCE_ROLES and base not in {READ_ONLY, VERIFY}:
        return VERIFY
    return base


def validate_agent_payload_against_schema(payload, schema) -> list[str]:
    if not isinstance(schema, dict) or not schema:
        return []
    issues: list[str] = []
    expected_type = schema.get("type")
    if expected_type and not _schema_type_matches(payload, str(expected_type)):
        issues.append(f"expected {expected_type}")
        return issues
    required = schema.get("required") or []
    if isinstance(required, list):
        for key in required:
            if isinstance(key, str) and (not isinstance(payload, dict) or key not in payload):
                issues.append(f"missing required field: {key}")
    properties = schema.get("properties") or {}
    if isinstance(payload, dict) and isinstance(properties, dict):
        for key, field_schema in properties.items():
            if key not in payload or not isinstance(field_schema, dict):
                continue
            field_type = field_schema.get("type")
            if field_type and not _schema_type_matches(payload[key], str(field_type)):
                issues.append(f"field {key} expected {field_type}")
    return issues


def _coerce_structured_text_payload(payload):
    if not isinstance(payload, dict):
        return payload
    for key in ("structured", "summary", "text"):
        raw = payload.get(key)
        if not isinstance(raw, str):
            continue
        text = raw.strip()
        if text.startswith("```"):
            lines = text.splitlines()
            if lines and lines[0].lstrip().startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            text = "\n".join(lines).strip()
        try:
            parsed = json.loads(text)
        except (TypeError, json.JSONDecodeError):
            continue
        if isinstance(parsed, (dict, list)):
            return parsed
    return payload


def _schema_type_matches(value, expected_type: str) -> bool:
    if expected_type == "object":
        return isinstance(value, dict)
    if expected_type == "array":
        return isinstance(value, list)
    if expected_type == "string":
        return isinstance(value, str)
    if expected_type == "number":
        return (isinstance(value, int) or isinstance(value, float)) and not isinstance(value, bool)
    if expected_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected_type == "boolean":
        return isinstance(value, bool)
    if expected_type == "null":
        return value is None
    return True


class AgentScheduler:
    def __init__(
        self,
        *,
        store: WorkflowStore,
        run: WorkflowRun,
        runner: ChildAgentRunner | None = None,
        config: SchedulerConfig | None = None,
        manage_run_completion: bool = True,
        args=None,
        cache_args=_CACHE_ARGS_UNSET,
    ):
        self.store = store
        self.run = run
        self.runner = runner or FakeChildAgentRunner()
        self.config = config or SchedulerConfig()
        self.manage_run_completion = bool(manage_run_completion)
        self.args = args
        self._has_explicit_cache_args = cache_args is not _CACHE_ARGS_UNSET
        self.cache_args = args if cache_args is _CACHE_ARGS_UNSET else cache_args
        self.workspace_path = None
        self.jobs = self.run.jobs
        self._stopping = False

    @property
    def running_count(self) -> int:
        return sum(1 for job in self.jobs if job.status == "running")

    @property
    def queued_count(self) -> int:
        return sum(1 for job in self.jobs if job.status == "queued")

    def register_agent(self, *, prompt: str, label: str | None = None, options: dict | None = None) -> WorkflowJob:
        options = normalize_agent_options(options)
        wave, wave_error = self._resolve_wave(options)
        max_allowed, reason = self._agent_limit(wave=wave)
        if wave_error:
            self._append("agent_rejected", payload={"reason": wave_error, "maxWaves": self._max_waves()})
            raise RuntimeError("workflow agent limit exceeded")
        if len(self.jobs) >= max_allowed:
            self._append("agent_rejected", payload={"reason": reason, "maxTotal": max_allowed})
            raise RuntimeError("workflow agent limit exceeded")
        workspace_path = self._sync_workspace_metadata()
        call_index = len(self.jobs)
        permission_profile = resolve_job_permission_profile(self.run.permission_profile, options)
        metadata = {
            "callIndex": call_index,
            "label": label,
            "options": options,
            "runId": self.run.run_id,
            "permissionProfile": permission_profile,
            "permissionPolicyVersion": self.run.permission_policy_version,
        }
        metadata["retryPolicy"] = normalize_retry_policy(metadata["options"].get("retryPolicy"))
        metadata["wave"] = wave
        metadata["dependsOn"] = [str(item) for item in (options.get("dependsOn") or []) if str(item)]
        if workspace_path:
            metadata["workspacePath"] = workspace_path
        job = WorkflowJob(
            job_id=f"agent_{call_index + 1}",
            prompt=prompt,
            status="queued",
            phase=options.get("phase"),
            metadata=metadata,
        )
        job.metadata["cacheKey"] = self._cache_key(job)
        self.jobs.append(job)
        self.store.save_run(self.run)
        self._append("agent_registered", job, {"cacheKey": job.metadata["cacheKey"], "label": label})
        self.store.write_workflow_progress(self.run)
        return job

    def register_cached_agent(self, *, prompt: str, label: str | None = None, options: dict | None = None, result: AgentResult, source_run_id: str | None = None, source_job_id: str | None = None) -> WorkflowJob:
        options = normalize_agent_options(options)
        wave, wave_error = self._resolve_wave(options)
        max_allowed, reason = self._agent_limit(wave=wave)
        if wave_error:
            self._append("agent_rejected", payload={"reason": wave_error, "maxWaves": self._max_waves()})
            raise RuntimeError("workflow agent limit exceeded")
        if len(self.jobs) >= max_allowed:
            self._append("agent_rejected", payload={"reason": reason, "maxTotal": max_allowed})
            raise RuntimeError("workflow agent limit exceeded")
        workspace_path = self._sync_workspace_metadata()
        call_index = len(self.jobs)
        permission_profile = resolve_job_permission_profile(self.run.permission_profile, options)
        metadata = {
            "callIndex": call_index,
            "label": label,
            "options": options,
            "runId": self.run.run_id,
            "permissionProfile": permission_profile,
            "permissionPolicyVersion": self.run.permission_policy_version,
            "result": sanitize(result.payload),
            "cachedFromRunId": source_run_id,
            "cachedFromJobId": source_job_id,
        }
        metadata["retryPolicy"] = normalize_retry_policy(metadata["options"].get("retryPolicy"))
        metadata["wave"] = wave
        metadata["dependsOn"] = [str(item) for item in (options.get("dependsOn") or []) if str(item)]
        if workspace_path:
            metadata["workspacePath"] = workspace_path
        job = WorkflowJob(
            job_id=f"agent_{call_index + 1}",
            prompt=prompt,
            status="cached",
            phase=options.get("phase"),
            metadata=metadata,
        )
        job.metadata["cacheKey"] = self._cache_key(job)
        cached_result = copy.deepcopy(result)
        cached_result.job_id = job.job_id
        if cached_result.transcript_ref:
            copied_ref = None
            if source_run_id:
                copied_ref = self.store.copy_agent_transcript(source_run_id, cached_result.transcript_ref, self.run, job)
            cached_result.transcript_ref = copied_ref
        if cached_result.transcript_ref:
            job.metadata["transcriptRef"] = cached_result.transcript_ref
        if cached_result.token_usage:
            job.metadata["tokenUsage"] = cached_result.token_usage
        if cached_result.tool_summary is not None:
            job.metadata["toolSummary"] = cached_result.tool_summary
        self.store.write_agent_result(self.run, job, cached_result)
        job.metadata["handoff"] = self._build_handoff(job, cached_result)
        self.jobs.append(job)
        self.store.save_run(self.run)
        self._append(
            "agent_cached",
            job,
            {
                "cacheKey": job.metadata["cacheKey"],
                "label": label,
                "sourceRunId": source_run_id,
                "sourceJobId": source_job_id,
                "resultRef": job.result_ref,
            },
        )
        self.store.write_workflow_progress(self.run)
        return job

    def _sync_workspace_metadata(self) -> str | None:
        workspace_path = normalize_workflow_workspace(self.args)
        if workspace_path is None and isinstance(self.run.metadata, dict):
            workspace_path = self.run.metadata.get("workspacePath")
        self.workspace_path = workspace_path
        if workspace_path:
            metadata = dict(self.run.metadata) if isinstance(self.run.metadata, dict) else {}
            metadata["workspacePath"] = workspace_path
            self.run.metadata = metadata
            self.store.save_run(self.run)
        return workspace_path

    def _max_waves(self) -> int:
        metadata = self.run.metadata if isinstance(self.run.metadata, dict) else {}
        orchestration = metadata.get("orchestration") if isinstance(metadata.get("orchestration"), dict) else {}
        try:
            return max(1, int(orchestration.get("maxWaves") or 10_000))
        except (TypeError, ValueError):
            return 10_000

    def _resolve_wave(self, options: dict) -> tuple[int, str | None]:
        dependencies = [str(item) for item in (options.get("dependsOn") or []) if str(item)]
        if not dependencies:
            return 1, None
        wave = 1
        for dependency in dependencies:
            upstream = next(
                (job for job in self.jobs if job.metadata.get("label") == dependency),
                None,
            )
            if upstream is None:
                return wave, "unknown_dependency"
            wave = max(wave, int(upstream.metadata.get("wave") or 1) + 1)
        if wave > self._max_waves():
            return wave, "delegation_max_waves_exceeded"
        return wave, None

    def _agent_limit(self, *, wave: int | None = None) -> tuple[int, str]:
        limit = self.config.max_total
        reason = "max_total_exceeded"
        metadata = self.run.metadata if isinstance(self.run.metadata, dict) else {}
        orchestration = metadata.get("orchestration") if isinstance(metadata.get("orchestration"), dict) else {}
        if orchestration.get("delegationAllowed") or metadata.get("mode") == "delegated":
            try:
                delegated_limit = int(orchestration.get("maxAgents") or limit)
            except (TypeError, ValueError):
                delegated_limit = limit
            if delegated_limit < limit:
                limit = max(1, delegated_limit)
                reason = "delegation_max_agents_exceeded"
        return limit, reason

    def tick(self, *, failure_policy: str = "continue") -> list[WorkflowJob]:
        completed: list[WorkflowJob] = []
        self._start_queued_jobs()
        for job in list(self.jobs):
            if job.status != "running":
                continue
            try:
                result = self.runner.poll(job)
            except Exception as exc:
                error = redact_sensitive_text(str(exc))
                if not self._schedule_retry(job, error):
                    self._fail_job(job, error)
                    completed.append(job)
                if failure_policy == "fail_fast":
                    if job.status == "failed":
                        self._fail_fast(error)
                continue
            if result is None:
                continue
            if result.status == "cancelled":
                self._cancel_job(job, reason="cancelled")
            elif result.status == "failed":
                error = redact_sensitive_text(str(result.payload.get("error") or "child agent failed"))
                if not self._schedule_retry(job, error, result=result):
                    self._fail_job(job, error, result=result)
                    completed.append(job)
                if failure_policy == "fail_fast" and job.status == "failed":
                    self._fail_fast(job.error or "child agent failed")
            else:
                result = self._apply_schema_contract(job, result)
                if result.status == "failed":
                    error = redact_sensitive_text(str(result.payload.get("error") or "child agent failed"))
                    if not self._schedule_retry(job, error, result=result):
                        self._fail_job(job, error, result=result)
                        completed.append(job)
                    if failure_policy == "fail_fast" and job.status == "failed":
                        self._fail_fast(job.error or "child agent failed")
                elif result.status == "degraded":
                    self._complete_job(job, result, status="degraded")
                    completed.append(job)
                else:
                    self._complete_job(job, result)
                    completed.append(job)
        if self.manage_run_completion:
            self._update_run_completion_state()
        self.store.save_run(self.run)
        self.store.write_workflow_progress(self.run)
        return completed

    def run_all(self, *, failure_policy: str = "continue") -> list[WorkflowJob]:
        completed: list[WorkflowJob] = []
        while any(job.status in {"queued", "running"} for job in self.jobs):
            before = [(job.job_id, job.status) for job in self.jobs]
            completed.extend(self.tick(failure_policy=failure_policy))
            after = [(job.job_id, job.status) for job in self.jobs]
            if before == after and any(job.status == "running" for job in self.jobs):
                continue
            if before == after and any(job.status == "queued" for job in self.jobs):
                due = [
                    float((job.metadata.get("retryPolicy") or {}).get("retryNotBefore") or 0)
                    for job in self.jobs
                    if job.status == "queued"
                ]
                if due and min(due) > time.time():
                    time.sleep(min(0.05, max(0.0, min(due) - time.time())))
                continue
        if self.manage_run_completion:
            self._update_run_completion_state()
            self.store.save_run(self.run)
        return completed

    def stop(self, *, reason: str = "") -> None:
        self._stopping = True
        self.run.status = "killed"
        self.run.error = redact_sensitive_text(reason) or None
        for job in list(self.jobs):
            if job.status == "queued":
                self._cancel_job(job, reason=reason or "stopped")
            elif job.status == "running":
                self.runner.cancel(job)
        self.store.save_run(self.run)

    def _prepare_declared_workspace(self, job: WorkflowJob) -> None:
        """Pre-create declared artifact parents before a child starts.

        ``file_write`` intentionally writes only files. Directory creation is a
        host responsibility so a child does not burn LLM turns guessing mkdir
        commands, and every created path still passes the workspace guard.
        """
        workspace = self.workspace_path or (self.run.metadata or {}).get("workspacePath")
        if not workspace:
            return
        options = job.metadata.get("options") if isinstance(job.metadata, dict) else {}
        paths: list[str] = []
        if isinstance(options, dict):
            for key in ("writeScope", "deliverables"):
                raw = options.get(key) or []
                if isinstance(raw, str):
                    raw = [raw]
                paths.extend(str(item) for item in raw if str(item).strip())
        for raw in paths:
            target = resolve_workspace_child(raw, Path(workspace))
            target.parent.mkdir(parents=True, exist_ok=True)

    def _start_queued_jobs(self) -> None:
        if self._stopping:
            return
        self._skip_blocked_jobs()
        slots = self.config.max_concurrent - self.running_count
        for job in self.jobs:
            if slots <= 0:
                return
            if job.status != "queued":
                continue
            if not self._dependencies_satisfied(job):
                continue
            retry_policy = job.metadata.get("retryPolicy") or {}
            retry_not_before = float(retry_policy.get("retryNotBefore") or 0)
            if retry_not_before > time.time():
                continue
            job.status = "running"
            retry_policy = normalize_retry_policy(retry_policy)
            retry_policy["attempts"] += 1
            retry_policy["retryNotBefore"] = None
            job.metadata["retryPolicy"] = retry_policy
            dependency_handoff = self._build_dependency_handoff(job)
            if dependency_handoff:
                job.metadata["dependencyHandoff"] = dependency_handoff
            else:
                job.metadata.pop("dependencyHandoff", None)
            try:
                self._prepare_declared_workspace(job)
                self.runner.start(job)
            except Exception as exc:
                self._fail_job(job, redact_sensitive_text(str(exc)) or "child startup failed")
                continue
            self._append("agent_started", job, {"label": job.metadata.get("label"), "wave": job.metadata.get("wave")})
            slots -= 1
        self.store.save_run(self.run)

    def _dependencies_satisfied(self, job: WorkflowJob) -> bool:
        for dependency in job.metadata.get("dependsOn") or []:
            upstream = next((item for item in self.jobs if item.metadata.get("label") == dependency), None)
            # A degraded upstream delivered a usable, if lower-fidelity,
            # result; the run is already marked degraded, so downstream work
            # may still consume it instead of being skipped wholesale.
            if upstream is None or upstream.status not in {"succeeded", "cached", "degraded"}:
                return False
        return True

    def _skip_blocked_jobs(self) -> None:
        for job in self.jobs:
            if job.status != "queued" or not job.metadata.get("dependsOn"):
                continue
            if self._dependencies_satisfied(job):
                continue
            if self._has_failed_dependency(job):
                job.status = "skipped"
                job.error = "dependency_failed"
                job.metadata["skipReason"] = "dependency_failed"
                self._append("agent_skipped", job, {"reason": "dependency_failed", "dependsOn": job.metadata.get("dependsOn")})

    def _has_failed_dependency(self, job: WorkflowJob) -> bool:
        for dependency in job.metadata.get("dependsOn") or []:
            upstream = next((item for item in self.jobs if item.metadata.get("label") == dependency), None)
            if upstream is not None and upstream.status in {"failed", "cancelled", "killed", "skipped", "stale"}:
                return True
        return False

    def _update_run_completion_state(self) -> None:
        if self.run.status != "running" or not self.jobs:
            return
        refresh_workflow_execution_metadata(self.run)
        settled = {"succeeded", "cached", "degraded"}
        if all(job.status in settled for job in self.jobs):
            metadata = dict(self.run.metadata) if isinstance(self.run.metadata, dict) else {}
            metadata.setdefault("integrationStatus", "pending")
            metadata.setdefault("finalAuditStatus", "pending")
            self.run.metadata = metadata
            self.run.status = "degraded" if any(job.status == "degraded" for job in self.jobs) else "succeeded"
            refresh_workflow_execution_metadata(self.run)
            self.store.write_final_result(
                self.run,
                {
                    "runId": self.run.run_id,
                    "status": self.run.status,
                    "workflowProgressRef": "workflow-progress.json",
                    "workflowIssues": sanitize(copy.deepcopy((self.run.metadata or {}).get("workflowIssues") or [])),
                    "childSummary": sanitize(copy.deepcopy((self.run.metadata or {}).get("childSummary") or {})),
                    "executionOutcome": (self.run.metadata or {}).get("executionOutcome"),
                    "integrationStatus": (self.run.metadata or {}).get("integrationStatus"),
                    "finalAuditStatus": (self.run.metadata or {}).get("finalAuditStatus"),
                    "jobs": [
                        {
                            "jobId": job.job_id,
                            "status": job.status,
                            "resultRef": job.result_ref,
                        }
                        for job in self.jobs
                    ],
                },
            )

    def _complete_job(self, job: WorkflowJob, result: AgentResult, *, status: str = "succeeded") -> None:
        job.status = status
        job.error = None
        if result.transcript_events:
            transcript_ref = self.store.write_agent_transcript(self.run, job, result.transcript_events)
            result.transcript_ref = result.transcript_ref or transcript_ref
        self.store.write_agent_result(self.run, job, result)
        handoff = self._build_handoff(job, result)
        job.metadata["result"] = result.payload
        job.metadata["handoff"] = handoff
        if result.transcript_ref:
            job.metadata["transcriptRef"] = result.transcript_ref
        if result.token_usage:
            job.metadata["tokenUsage"] = result.token_usage
        if result.tool_summary is not None:
            job.metadata["toolSummary"] = result.tool_summary
        self._append_permission_events_from_result(job, result)
        self._record_observed_mutations(job, result)
        self._record_observed_artifacts(job, result)
        self._append(
            "agent_completed",
            job,
            {"resultRef": job.result_ref, "status": job.status, "result": self._event_result_summary(result)},
        )

    @staticmethod
    def _compact_handoff_summary(payload: dict, error: str | None = None) -> str:
        raw = payload.get("summary") or payload.get("text") or payload.get("error") or error or ""
        text = str(raw).strip()
        # Prefer the final turn when native child output includes tool-call logs.
        marker = '\nTurn '
        last = text.rfind(marker)
        if last >= 0:
            text = text[last:].lstrip('\n ')
        rows = [line for line in text.splitlines() if not line.lstrip().startswith(("🔨", "Tool call:", "tool_call:"))]
        cleaned = redact_sensitive_text('\n'.join(rows).strip())
        # Bounding is structure-aware: a structured answer is projected into a
        # valid, smaller JSON value instead of being cut mid-object. A fixed
        # character cut is what made a synthesis child read half of
        # ``sources[0]`` and then report the upstream sources as unrecoverable.
        return bounded_structured_summary(cleaned, ref=payload.get("resultRef"))

    @staticmethod
    def _artifact_refs_from_payload(payload: dict) -> list[str]:
        """Extract only small, workspace-relative artifact references.

        A child result may contain a large ``text`` field or tool transcript.
        Handoffs expose paths, never those bodies.
        """
        refs: list[str] = []
        keys = {"artifacts", "artifactRef", "artifactRefs", "artifactPaths", "deliverables", "path"}

        def visit(value, key: str = ""):
            if isinstance(value, dict):
                for child_key, child in value.items():
                    if child_key in keys:
                        visit(child, child_key)
            elif isinstance(value, (list, tuple)):
                for child in value:
                    visit(child, key)
            elif isinstance(value, str) and key in keys:
                candidate = value.strip().replace("\\", "/")
                if not candidate or candidate.startswith(("/", "\\")) or "://" in candidate:
                    return
                if candidate.startswith("../") or candidate == ".." or "/../" in candidate:
                    return
                # Semantic deliverable names are not paths; extensions or a slash
                # are sufficient to distinguish actual workspace artifacts.
                if "/" not in candidate and "." not in candidate:
                    return
                if candidate not in refs:
                    refs.append(candidate[:300])

        visit(payload)
        return refs[:32]

    def _build_handoff(self, job: WorkflowJob, result: AgentResult, *, error: str | None = None) -> dict:
        payload = result.payload if isinstance(result.payload, dict) else {}
        summary = payload.get("summary") or payload.get("text") or payload.get("error") or error or ""
        evidence = payload.get("evidence")
        if not isinstance(evidence, list):
            evidence = []
        blocking = payload.get("blockingIssues")
        if not isinstance(blocking, list):
            blocking = []
        options = job.metadata.get("options") if isinstance(job.metadata, dict) else {}
        declared = options.get("deliverables") if isinstance(options, dict) else []
        # Real paths first: payload/observed writes are resolvable, whereas the
        # plan's semantic labels ("sources", "synthesis") are not paths at all.
        artifact_refs = self._artifact_refs_from_payload(payload)
        for ref in self._observed_artifact_refs(job, payload):
            if ref not in artifact_refs:
                artifact_refs.append(ref)
        artifact_refs.extend(ref for ref in self._artifact_refs_from_payload({"deliverables": declared}) if ref not in artifact_refs)
        readable = self._materialize_result_copy(job)
        handoff = {
            "status": result.status,
            "summary": self._compact_handoff_summary(payload, error),
            "evidence": sanitize(copy.deepcopy(evidence[:8])),
            "blockingIssues": sanitize(copy.deepcopy(blocking[:8])),
            # These are logical refs for audit/recovery. They are not expanded into
            # the next LLM request; workspace artifactRefs are the only read path.
            "resultRef": job.result_ref,
            # A workspace-relative, readable copy of the durable result. Readers
            # that only know ``workspacePath`` can open this; ``resultRef`` alone
            # is unresolvable for them.
            "readableResultRef": readable[0] if readable else None,
            "resultPath": readable[1] if readable else None,
            "artifactRefs": artifact_refs,
            # Ownership of the files this job wrote. A downstream reader can judge
            # whose data it is reading, and a path claimed by another job is
            # visible rather than silently attributed to whoever wrote last.
            "artifactOwners": self._artifact_owners_for(job, artifact_refs),
            "transcriptRef": result.transcript_ref,
        }
        handoff_ref = self._write_handoff_artifact(job, result, handoff)
        if handoff_ref:
            handoff["handoffRef"] = handoff_ref
        return handoff

    def _artifact_owners_for(self, job: WorkflowJob, refs: list[str]) -> dict[str, list[str]]:
        """Map each artifact path to the job(s) that wrote it, run-wide.

        ``artifactRefs`` alone says what exists; the reader still cannot tell
        whether ``report.md`` came from the research stage or the synthesis
        stage, nor that two jobs both wrote it. Ownership is already recorded at
        diff time, so this only unions it across the run -- no tool-name
        inference and no new bookkeeping.
        """
        owners: dict[str, list[str]] = {}
        for item in self.run.jobs:
            metadata = item.metadata if isinstance(item.metadata, dict) else {}
            for entry in workspace_writes_with_writer(metadata.get("observedArtifacts")):
                writer = entry.get("writer") or item.job_id
                bucket = owners.setdefault(entry["path"], [])
                if writer not in bucket:
                    bucket.append(writer)
        return {ref: owners[ref] for ref in refs if ref in owners}


    def _observed_artifact_refs(self, job: WorkflowJob, payload: dict) -> list[str]:
        """Return only observed/declared files that exist inside the workspace."""
        metadata = job.metadata if isinstance(job.metadata, dict) else {}
        workspace = self.workspace_path or (self.run.metadata or {}).get("workspacePath")
        if not isinstance(workspace, str) or not workspace:
            return []
        candidates = observed_artifact_paths(metadata.get("observedArtifacts"))
        options = metadata.get("options") if isinstance(metadata.get("options"), dict) else {}
        for key in ("deliverables", "writeScope"):
            raw = (options or {}).get(key) or []
            if isinstance(raw, str):
                raw = [raw]
            candidates.extend(str(item) for item in raw if str(item).strip())
        resolved: list[str] = []
        for candidate in candidates:
            try:
                ref = normalize_workspace_relative(candidate, Path(workspace))
                target = resolve_workspace_child(ref, Path(workspace))
            except (WorkspacePathError, OSError, ValueError):
                continue
            if target.is_file() and ref not in resolved:
                resolved.append(ref)
        return resolved[:32]

    def _write_handoff_artifact(self, job: WorkflowJob, result: AgentResult, handoff: dict) -> str | None:
        """Persist detailed child output in the workspace, never in the next prompt."""
        workspace = self.workspace_path or (self.run.metadata or {}).get("workspacePath")
        if not workspace:
            return None
        ref = f"workflow-handoffs/{job.job_id}.json"
        try:
            target = resolve_workspace_child(ref, Path(workspace))
            artifact_dir = getattr(self.run, "artifact_dir", None)
            envelope = {
                "version": 1,
                "jobId": job.job_id,
                "label": job.metadata.get("label"),
                "status": result.status,
                "summary": handoff.get("summary"),
                # Refs use two different roots, and this file is read directly by
                # the GA agent after the run. State both bases so nobody resolves
                # ``resultRef`` under the run workspace and concludes the durable
                # result is gone: ``artifactRefs`` are relative to
                # ``workspacePath``; ``resultRef``/``transcriptRef`` are
                # run-internal and relative to ``runArtifactDir``.
                "workspacePath": str(workspace),
                "runArtifactDir": str(artifact_dir) if artifact_dir else None,
                "resultRef": handoff.get("readableResultRef") or handoff.get("resultRef"),
                "resultPath": handoff.get("resultPath") or _absolute_under(artifact_dir, handoff.get("resultRef")),
                "runInternalResultRef": handoff.get("resultRef"),
                "transcriptRef": handoff.get("transcriptRef"),
                "transcriptPath": _absolute_under(artifact_dir, handoff.get("transcriptRef")),
                "artifactRefs": handoff.get("artifactRefs") or [],
                # Detailed research belongs in declared workspace artifacts. Keep
                # this control-plane file bounded; never mirror payload.text here.
            }
            atomic_write_json(target, envelope)
            return ref
        except (WorkspacePathError, OSError, TypeError, ValueError):
            return None

    def _materialize_result_copy(self, job: WorkflowJob) -> tuple[str, str] | None:
        """Copy a job's durable result into the run workspace.

        ``resultRef`` is relative to the run's *internal* artifact directory, not
        to the run workspace. A reader that joins it onto ``workspacePath``
        therefore builds a real, well-formed path that does not exist -- which is
        exactly how a correct run reported its durable result as missing. The
        copy lands in ``workflow-handoffs/``, the host-owned directory child
        diffs ignore, so writing it while other children still run cannot be
        misattributed to whichever child finishes next.

        Returns ``(workspace_relative_ref, absolute_path)`` or ``None``.
        """

        ref = job.result_ref
        artifact_dir = getattr(self.run, "artifact_dir", None)
        workspace = self.workspace_path or (self.run.metadata or {}).get("workspacePath")
        if not ref or not artifact_dir or not workspace:
            return None
        source = Path(artifact_dir) / ref
        if not source.is_file():
            return None
        target_ref = f"workflow-handoffs/result-{job.job_id}.json"
        try:
            payload = json.loads(source.read_text(encoding="utf-8", errors="replace"))
            target = resolve_workspace_child(target_ref, Path(workspace))
            atomic_write_json(target, sanitize(payload))
        except (WorkspacePathError, OSError, ValueError, TypeError):
            return None
        return target_ref, str(target)

    def _build_dependency_handoff(self, job: WorkflowJob) -> list[dict]:
        handoffs: list[dict] = []
        for dependency in job.metadata.get("dependsOn") or []:
            upstream = next((item for item in self.jobs if item.metadata.get("label") == dependency), None)
            if upstream is None:
                continue
            raw = upstream.metadata.get("handoff") or {}
            if not isinstance(raw, dict):
                raw = {}
            handoffs.append({
                "label": upstream.metadata.get("label") or dependency,
                "status": upstream.status,
                "summary": self._compact_handoff_summary(raw, None),
                "evidence": sanitize(copy.deepcopy(raw.get("evidence") or []))[:8],
                "blockingIssues": sanitize(copy.deepcopy(raw.get("blockingIssues") or []))[:8],
                "resultRef": raw.get("resultRef") or upstream.result_ref,
                "upstreamResultPath": self._materialize_upstream_result(upstream),
                "artifactRefs": [str(ref) for ref in (raw.get("artifactRefs") or []) if str(ref)][:32],
                "artifactOwners": sanitize(copy.deepcopy(raw.get("artifactOwners") or {}))
                if isinstance(raw.get("artifactOwners"), dict) else {},
                "handoffRef": raw.get("handoffRef"),
                "transcriptRef": raw.get("transcriptRef") or upstream.metadata.get("transcriptRef"),
            })
        return handoffs

    def _materialize_upstream_result(self, upstream: WorkflowJob) -> str | None:
        """Copy an upstream job's durable result into the run workspace.

        ``resultRef`` lives in the run's internal artifact directory, and a
        workflow child runs under a path limit that only exposes its workspace,
        so a dependent child can never open it. Advertising a ref the child
        cannot read is how the synthesis stage concluded the upstream sources
        were "gone". The host therefore writes a readable copy next to the
        other control-plane files and hands the child that workspace-relative
        path.

        The copy happens before the child starts, so the child's own
        before/after workspace diff never attributes it to the downstream job.
        """
        ref = upstream.result_ref
        artifact_dir = getattr(self.run, "artifact_dir", None)
        workspace = self.workspace_path or (self.run.metadata or {}).get("workspacePath")
        if not ref or not artifact_dir or not workspace:
            return None
        source = Path(artifact_dir) / ref
        try:
            if not source.is_file():
                return None
            payload = json.loads(source.read_text(encoding="utf-8", errors="replace"))
        except (OSError, ValueError):
            return None
        target_ref = f"workflow-handoffs/upstream-{upstream.job_id}.json"
        try:
            atomic_write_json(resolve_workspace_child(target_ref, Path(workspace)), sanitize(payload))
        except (WorkspacePathError, OSError, TypeError, ValueError):
            return None
        return target_ref

    def downstream_result(self, job: WorkflowJob) -> dict:
        """Return the bounded value exposed to the workflow script/next child.

        Durable result.json and transcript files retain full audit data, but the
        RPC boundary must not put a child transcript into the next LLM request.
        A schema-constrained job additionally exposes its validated structured
        value, because that value *is* the deliverable the script asked for
        (Step-Code returns the validated value straight from ``agent()``); the
        raw ``text`` transcript field is always dropped.
        """
        handoff = job.metadata.get("handoff") if isinstance(job.metadata, dict) else None
        if not isinstance(handoff, dict):
            handoff = {}
        result = {
            "status": job.status,
            "summary": handoff.get("summary") or "",
            "resultRef": handoff.get("resultRef") or job.result_ref,
            "handoffRef": handoff.get("handoffRef"),
            "artifactRefs": list(handoff.get("artifactRefs") or []),
            "artifactOwners": sanitize(copy.deepcopy(handoff.get("artifactOwners") or {}))
            if isinstance(handoff.get("artifactOwners"), dict) else {},
            "transcriptRef": handoff.get("transcriptRef") or job.metadata.get("transcriptRef"),
            "evidence": sanitize(copy.deepcopy(handoff.get("evidence") or []))[:8],
            "blockingIssues": sanitize(copy.deepcopy(handoff.get("blockingIssues") or []))[:8],
        }
        payload = job.metadata.get("result") if isinstance(job.metadata, dict) else {}
        if isinstance(payload, dict):
            for key in ("verificationPassed", "schemaFallback", "schemaValidation", "statusCode", "category", "providerAnomaly"):
                if key in payload:
                    result[key] = sanitize(copy.deepcopy(payload[key]))
            result.update(self._validated_structured_fields(job, payload))
        validation = job.metadata.get("schemaValidation") if isinstance(job.metadata, dict) else None
        if isinstance(validation, dict) and "schemaValidation" not in result:
            result["schemaValidation"] = sanitize(copy.deepcopy(validation))
        return result

    def _validated_structured_fields(self, job: WorkflowJob, payload: dict) -> dict:
        """Expose declared schema fields to the script once validation passed."""
        options = job.metadata.get("options") if isinstance(job.metadata, dict) else None
        schema = (options or {}).get("schema") if isinstance(options, dict) else None
        if not isinstance(schema, dict) or not schema:
            return {}
        validation = job.metadata.get("schemaValidation") if isinstance(job.metadata, dict) else None
        if isinstance(validation, dict) and validation.get("ok") is not True:
            return {}
        declared = schema.get("properties")
        names = list(declared.keys()) if isinstance(declared, dict) and declared else list(schema.get("required") or [])
        exposed: dict = {}
        for name in names:
            key = str(name)
            if key in {"text", "summary"} or key not in payload:
                continue
            exposed[key] = sanitize(copy.deepcopy(payload[key]))
        return exposed

    def _schedule_retry(self, job: WorkflowJob, error: str, *, result: AgentResult | None = None) -> bool:
        policy = normalize_retry_policy(job.metadata.get("retryPolicy"))
        text_parts = [str(error or "").lower()]
        if result is not None and isinstance(result.payload, dict):
            text_parts.extend(str(result.payload.get(key) or "").lower() for key in ("code", "category", "providerAnomaly"))
        text = " ".join(text_parts)
        retryable = any(pattern and pattern in text for pattern in policy["retryableErrors"])
        if not retryable or policy["attempts"] >= policy["maxAttempts"]:
            return self._schedule_repair(job, error, policy)
        feedback = self._schema_retry_feedback(job, result)
        policy["lastError"] = redact_sensitive_text(error)
        policy["retryNotBefore"] = time.time() + (policy["backoffMs"] / 1000.0)
        job.metadata["retryPolicy"] = policy
        if feedback:
            job.metadata["retryFeedback"] = feedback
        else:
            job.metadata.pop("retryFeedback", None)
        job.status = "queued"
        job.error = None
        self._append(
            "agent_retry_scheduled",
            job,
            {
                "attempt": policy["attempts"],
                "nextAttempt": policy["attempts"] + 1,
                "maxAttempts": policy["maxAttempts"],
                "error": policy["lastError"],
                "backoffMs": policy["backoffMs"],
            },
        )
        return True

    @staticmethod
    def _schema_retry_feedback(job: WorkflowJob, result: AgentResult | None) -> dict | None:
        """Carry the previous schema rejection into the retried child prompt.

        Re-running the identical prompt just reproduces the identical failure.
        Step-Code appends ``<workflow-retry>`` with ``lastErrors`` for the same
        reason; a retry must be told what was wrong.
        """
        validation = job.metadata.get("schemaValidation") if isinstance(job.metadata, dict) else None
        issues = validation.get("issues") if isinstance(validation, dict) else None
        if not isinstance(issues, list) or not issues:
            payload = result.payload if result is not None and isinstance(result.payload, dict) else {}
            nested = payload.get("schemaValidation") if isinstance(payload.get("schemaValidation"), dict) else {}
            issues = nested.get("issues")
        if not isinstance(issues, list):
            return None
        cleaned = [str(item).strip()[:300] for item in issues if str(item).strip()]
        if not cleaned:
            return None
        policy = normalize_retry_policy(job.metadata.get("retryPolicy"))
        return {
            "attempt": int(policy.get("attempts") or 0) + 1,
            "issues": cleaned[:12],
        }

    def _schedule_repair(self, job: WorkflowJob, error: str, policy: dict) -> bool:
        repair_role = str(policy.get("repairRole") or "").strip()
        if not repair_role:
            return False
        if int(policy.get("repairAttempts") or 0) >= 1:
            return False
        max_allowed, _reason = self._agent_limit(wave=int(job.metadata.get("wave") or 1))
        if len(self.jobs) >= max_allowed:
            return False
        self._sync_workspace_metadata()
        call_index = len(self.jobs)
        repair_policy = normalize_retry_policy({"maxAttempts": 1})
        failed_options = job.metadata.get("options") if isinstance(job.metadata.get("options"), dict) else {}
        validation = job.metadata.get("schemaValidation") if isinstance(job.metadata.get("schemaValidation"), dict) else {}
        issues = [str(item).strip() for item in (validation.get("issues") or []) if str(item).strip()]
        metadata = {
            "callIndex": call_index,
            "label": repair_role,
            "options": {
                "phase": job.phase,
                "role": repair_role,
                "repairOf": job.job_id,
                "schema": copy.deepcopy(failed_options.get("schema")),
            },
            "runId": self.run.run_id,
            "permissionProfile": self.run.permission_profile,
            "permissionPolicyVersion": self.run.permission_policy_version,
            "retryPolicy": repair_policy,
            "wave": int(job.metadata.get("wave") or 1),
            "dependsOn": [],
            "repairRole": repair_role,
            "repairOf": job.job_id,
        }
        if not metadata["options"]["schema"]:
            metadata["options"].pop("schema", None)
        elif issues:
            metadata["retryFeedback"] = {"attempt": 1, "issues": issues[:12]}
        workspace_path = self.workspace_path
        if workspace_path:
            metadata["workspacePath"] = workspace_path
        prompt = (
            f"The upstream workflow step {job.job_id} was rejected before this repair. "
            f"Failure: {redact_sensitive_text(error)[:1_000]}.\n"
            "Complete the original assignment and produce the required output. Schema and "
            "workspace rules are enforced by the host; an identical failing answer is not a repair.\n\n"
            "Original assignment:\n"
            f"{(job.prompt or '')[:4_000]}"
        )
        repair_job = WorkflowJob(
            job_id=f"agent_{call_index + 1}",
            prompt=prompt,
            status="queued",
            phase=job.phase,
            metadata=metadata,
        )
        repair_job.metadata["cacheKey"] = self._cache_key(repair_job)
        policy["repairAttempts"] = int(policy.get("repairAttempts") or 0) + 1
        job.metadata["retryPolicy"] = policy
        self.jobs.append(repair_job)
        self._append(
            "agent_repair_scheduled",
            repair_job,
            {
                "repairOf": job.job_id,
                "repairRole": repair_role,
                "error": redact_sensitive_text(error)[:1_000],
            },
        )
        return False

    def _apply_schema_contract(self, job: WorkflowJob, result: AgentResult) -> AgentResult:
        options = job.metadata.get("options") or {}
        schema = options.get("schema")
        if not isinstance(schema, dict) or not schema:
            return result
        issues = validate_agent_payload_against_schema(result.payload, schema)
        if issues:
            parsed_payload = _coerce_structured_text_payload(result.payload)
            if parsed_payload is not result.payload:
                result.payload = parsed_payload
                issues = validate_agent_payload_against_schema(result.payload, schema)
        if not issues:
            job.metadata["schemaValidation"] = {
                "ok": True,
                "code": None,
                "issues": [],
                "fallback": None,
                "fallbackApplied": False,
            }
            # A retry (or a repair) satisfied the contract, so the earlier
            # rejection is resolved. Leaving it in ``workflowIssues`` made the
            # runtime degrade a run whose every job ended ``succeeded``, because
            # degradation is decided by "any outstanding issue" -- the record is
            # history, not an open problem.
            self._clear_schema_issue(job)
            return result
        fallback = str(options.get("fallback") or "").strip().lower()
        fallback_applied = fallback == "text"
        validation = {
            "ok": False,
            "code": SCHEMA_VALIDATION_FAILED,
            "issues": issues,
            "fallback": fallback or None,
            "fallbackApplied": fallback_applied,
        }
        job.metadata["schemaValidation"] = validation
        self._record_workflow_issue(job, validation)
        if fallback_applied:
            payload = copy.deepcopy(result.payload or {})
            payload["schemaFallback"] = True
            payload["schemaValidation"] = copy.deepcopy(validation)
            result.payload = payload
            # A schema miss that was absorbed by a text fallback is a real
            # degradation, not a success. Mark the terminal state accordingly
            # so the run cannot report a clean success.
            result.status = "degraded"
            return result
        error = f"{SCHEMA_VALIDATION_FAILED}: " + "; ".join(issues)
        result.status = "failed"
        result.payload = {
            "error": error,
            "code": SCHEMA_VALIDATION_FAILED,
            "schemaValidation": copy.deepcopy(validation),
        }
        return result

    def _clear_schema_issue(self, job: WorkflowJob) -> None:
        """Drop this job's resolved ``schema_validation_failed`` record.

        Only issues for this job are removed, and only the schema code, so a
        different job's failure or an unrelated run-level issue is untouched. The
        retry history itself stays in ``retryPolicy.lastError`` and the
        ``agent_retry_scheduled`` journal event, which are not degradation gates.
        """
        metadata = self.run.metadata if isinstance(self.run.metadata, dict) else None
        if metadata is None:
            return
        existing = metadata.get("workflowIssues")
        if not isinstance(existing, list):
            return
        remaining = [
            issue
            for issue in existing
            if not (
                isinstance(issue, dict)
                and issue.get("code") == SCHEMA_VALIDATION_FAILED
                and issue.get("jobId") == job.job_id
            )
        ]
        if len(remaining) == len(existing):
            return
        metadata["workflowIssues"] = remaining
        self.run.metadata = metadata
        self._append("workflow_issue_resolved", job, {"code": SCHEMA_VALIDATION_FAILED})

    def _record_workflow_issue(self, job: WorkflowJob, validation: dict) -> None:
        issue = {
            "code": SCHEMA_VALIDATION_FAILED,
            "type": SCHEMA_VALIDATION_FAILED,
            "jobId": job.job_id,
            "agentLabel": job.metadata.get("label"),
            "retryable": True,
            "fallback": validation.get("fallback"),
            "fallbackUsed": validation.get("fallback") if validation.get("fallbackApplied") else None,
            "fallbackApplied": bool(validation.get("fallbackApplied")),
            "issues": copy.deepcopy(validation.get("issues") or []),
        }
        metadata = self.run.metadata if isinstance(self.run.metadata, dict) else {}
        workflow_issues = metadata.setdefault("workflowIssues", [])
        workflow_issues.append(issue)
        self.run.metadata = metadata
        self._append("workflow_issue", job, issue)

    def record_capability_degradation(self, *, unavailable, missing_tools, job=None) -> None:
        """Record a missing capability as a *visible degradation*, not a failure.

        A disconnected MCP server is an environment fact, not a model mistake.
        Step-Code simply never constructs the tool; Codex emits an
        ``McpStartupUpdate`` with a reason and keeps the servers that did start.
        Recording it in ``workflowIssues`` makes the terminal state ``degraded``
        so the run can never report a clean pass while a declared capability was
        absent.
        """

        unavailable = [str(item) for item in (unavailable or []) if str(item)]
        missing_tools = [str(item) for item in (missing_tools or []) if str(item)]
        if not unavailable and not missing_tools:
            return
        metadata = self.run.metadata if isinstance(self.run.metadata, dict) else {}
        issues = list(metadata.get("workflowIssues") or [])
        parts = []
        if unavailable:
            parts.append("capabilities without a connected tool: " + ", ".join(unavailable))
        if missing_tools:
            parts.append("declared tools not present in the environment: " + ", ".join(missing_tools))
        issue = {
            "code": "capability_unavailable",
            "message": "; ".join(parts),
            "unavailableCapabilities": sorted(set(unavailable)),
            "missingTools": sorted(set(missing_tools)),
        }
        duplicate = any(
            isinstance(item, dict)
            and item.get("code") == "capability_unavailable"
            and item.get("unavailableCapabilities") == issue["unavailableCapabilities"]
            and item.get("missingTools") == issue["missingTools"]
            for item in issues
        )
        if not duplicate:
            issues.append(issue)
        metadata["workflowIssues"] = issues
        metadata["unavailableCapabilities"] = sorted(
            {*(metadata.get("unavailableCapabilities") or []), *unavailable}
        )
        if missing_tools:
            metadata["missingRequiredTools"] = sorted(
                {*(metadata.get("missingRequiredTools") or []), *missing_tools}
            )
        self.run.metadata = metadata
        self._append("capability_unavailable", job, issue)

    def _event_result_summary(self, result: AgentResult) -> dict:
        summary = {
            "jobId": result.job_id,
            "status": result.status,
            "transcriptRef": result.transcript_ref,
            "tokenUsage": result.token_usage,
            "toolSummary": result.tool_summary,
        }
        payload = result.payload or {}
        payload_summary = {
            key: payload[key]
            for key in ("summary", "error", "statusCode", "category", "providerAnomaly")
            if key in payload
        }
        if payload_summary:
            summary["payload"] = payload_summary
        return summary

    def _fail_job(self, job: WorkflowJob, error: str, result: AgentResult | None = None) -> None:
        job.status = "failed"
        error = redact_sensitive_text(error)
        job.error = error
        payload = {"error": error}
        if result is not None:
            if result.transcript_events:
                transcript_ref = self.store.write_agent_transcript(self.run, job, result.transcript_events)
                result.transcript_ref = result.transcript_ref or transcript_ref
            self.store.write_agent_result(self.run, job, result)
            job.metadata["result"] = result.payload
            job.metadata["handoff"] = self._build_handoff(job, result, error=error)
            if result.transcript_ref:
                job.metadata["transcriptRef"] = result.transcript_ref
            if result.token_usage:
                job.metadata["tokenUsage"] = result.token_usage
            if result.tool_summary is not None:
                job.metadata["toolSummary"] = result.tool_summary
            payload["resultRef"] = job.result_ref
            payload["result"] = self._event_result_summary(result)
            self._append_permission_events_from_result(job, result)
            self._record_observed_mutations(job, result)
        self._append("agent_failed", job, payload)

    def _record_observed_mutations(self, job: WorkflowJob, result: AgentResult) -> None:
        """Record write-capability from observed tool calls, not from declarations.

        Real runs showed the model omitting ``role``/``writeScope`` entirely while
        still writing files, so "did this packet mutate state" cannot be
        predicted from the plan. It can be observed: when a child actually
        invokes a mutating tool, that is recorded as a fact on the job and the
        run. Downstream gates read this instead of guessing.
        """

        from workflow_permissions import MUTATING_TOOL_NAMES

        allowing = {"allow", "ask"}
        observed: list[str] = []
        for event in result.transcript_events:
            if event.get("type") != "tool_allowed":
                continue
            tool_name = str(event.get("toolName") or event.get("tool_name") or "").strip()
            if tool_name not in MUTATING_TOOL_NAMES:
                continue
            decision = event.get("decision") or (event.get("permission") or {}).get("action")
            if decision and str(decision) not in allowing:
                continue
            if tool_name not in observed:
                observed.append(tool_name)
        # Filesystem evidence outranks the tool allowlist: a writer tool we have
        # not enumerated yet still mutates state, and code_run can too.
        if (result.tool_summary or {}).get("writtenPaths") and not observed:
            observed.append("workspace_write")
        if not observed:
            return
        job.metadata["observedMutations"] = observed
        metadata = dict(self.run.metadata) if isinstance(self.run.metadata, dict) else {}
        labels = list(metadata.get("observedMutationAgents") or [])
        if job.job_id not in labels:
            labels.append(job.job_id)
        metadata["observedMutationAgents"] = labels
        metadata["observedMutationTools"] = sorted({*(metadata.get("observedMutationTools") or []), *observed})
        self.run.metadata = metadata
        self.store.save_run(self.run)
        self._append("state_mutation_observed", job, {"toolNames": observed})

    def _record_observed_artifacts(self, job: WorkflowJob, result: AgentResult) -> None:
        """Record the workspace-relative files a child actually wrote.

        The plan's ``artifacts`` entries are semantic labels ("sources",
        "synthesis"), not paths, so a handoff built from them alone tells the
        next reader nothing and the LLM has to guess a location. The child's
        own filesystem changes are the ground truth. The runner reports those as
        a before/after workspace diff, which stays correct for ``file_write``,
        ``code_run``, and any writer tool added later; no tool-name list here.
        """
        metadata = self.run.metadata if isinstance(self.run.metadata, dict) else {}
        workspace = self.workspace_path or metadata.get("workspacePath")
        if not workspace:
            return
        observed = (result.tool_summary or {}).get("writtenPaths") or []
        written: list[str] = []
        for raw in observed:
            try:
                ref = normalize_workspace_relative(str(raw), Path(workspace))
            except WorkspacePathError:
                continue
            if ref not in written:
                written.append(ref)
        if not written:
            return
        # Record which job wrote each path. The diff is the only place that
        # knows both facts at once; deriving the owner later would mean reading
        # tool names again, which is the enumeration this design removed.
        writer = str(job.metadata.get("label") or job.job_id)
        existing = workspace_writes_with_writer(job.metadata.get("observedArtifacts"))
        merged = {entry["path"]: entry for entry in existing}
        for ref in written:
            entry = merged.setdefault(ref, {"path": ref, "writer": writer})
            if not entry.get("writer"):
                entry["writer"] = writer
        job.metadata["observedArtifacts"] = [merged[ref] for ref in written if ref in merged] + [
            entry for ref, entry in merged.items() if ref not in set(written)
        ]
        self._record_artifact_collisions(job)
        # Persist the run-level ``path -> writers`` index. Jobs that run in a
        # child process keep no in-process handoff dict, so consumers read this
        # from run metadata instead of re-deriving ownership themselves.
        run_metadata = dict(self.run.metadata) if isinstance(self.run.metadata, dict) else {}
        ownership = build_artifact_ownership_index(self.run)
        if ownership:
            run_metadata["artifactOwnership"] = ownership
        self.run.metadata = run_metadata
        self.store.save_run(self.run)
        # Publish the progress snapshot immediately: the Ink panel polls this
        # file, and waiting for the end-of-batch write made a finished artifact
        # invisible while later jobs were still running.
        self.store.write_workflow_progress(self.run)
        self._append("artifact_written", job, {"paths": written[:32]})

    def _record_artifact_collisions(self, job: WorkflowJob) -> None:
        """Flag paths written by more than one job in this run.

        Two children writing ``report.md`` means the second silently overwrote
        the first; without per-path ownership the handoff could only hand over
        one opaque path. The run stays runnable, but the collision is recorded so
        it is visible instead of being discovered from file contents later.
        """
        owners: dict[str, list[str]] = {}
        for item in self.run.jobs:
            metadata = item.metadata if isinstance(item.metadata, dict) else {}
            for entry in workspace_writes_with_writer(metadata.get("observedArtifacts")):
                writer = entry.get("writer") or item.job_id
                bucket = owners.setdefault(entry["path"], [])
                if writer not in bucket:
                    bucket.append(writer)
        collisions = {path: writers for path, writers in owners.items() if len(writers) > 1}
        if not collisions:
            return
        metadata = dict(self.run.metadata) if isinstance(self.run.metadata, dict) else {}
        issues = list(metadata.get("workflowIssues") or [])
        for path, writers in sorted(collisions.items()):
            issue = {
                "code": "artifact_path_collision",
                "message": f"{path} was written by multiple jobs: {', '.join(writers)}",
                "path": path,
                "writers": writers,
            }
            if not any(isinstance(item, dict) and item.get("code") == issue["code"] and item.get("path") == path for item in issues):
                issues.append(issue)
        metadata["workflowIssues"] = issues
        metadata["artifactCollisions"] = {path: writers for path, writers in sorted(collisions.items())}
        self.run.metadata = metadata
        self._append("artifact_collision", job, {"collisions": metadata["artifactCollisions"]})

    def _append_permission_events_from_result(self, job: WorkflowJob, result: AgentResult) -> None:
        for event in result.transcript_events:
            if event.get("type") not in {"permission_profile_selected", "tool_allowed", "tool_denied"}:
                continue
            self.store.append_permission_event(self.run, {**event, "jobId": event.get("jobId") or job.job_id})

    def _cancel_job(self, job: WorkflowJob, *, reason: str) -> None:
        if job.status in {"succeeded", "degraded", "failed", "cancelled", "cached", "skipped"}:
            return
        job.status = "cancelled"
        job.error = reason or None
        self._append("agent_cancelled", job, {"reason": redact_sensitive_text(reason or "")})

    def _fail_fast(self, error: str) -> None:
        self.run.status = "failed"
        self.run.error = redact_sensitive_text(error)
        for job in list(self.jobs):
            if job.status == "queued":
                self._cancel_job(job, reason="fail_fast")
            elif job.status == "running":
                self.runner.cancel(job)
                self._cancel_job(job, reason="fail_fast")

    def _cache_key(self, job: WorkflowJob) -> dict:
        options = job.metadata.get("options") or {}
        metadata = self.run.metadata or {}
        return {
            "scriptHash": _stable_hash(self.run.script),
            "argsHash": _stable_hash(self.cache_args if self._has_explicit_cache_args else self.args),
            # Each run own workspace lives under a per-run directory, so hashing
            # the run-local path would invalidate every cached prefix on resume.
            # The base root is the stable identity of "which workspace family
            # this run belongs to"; the run directory is an implementation
            # detail of isolation.
            "workspacePathHash": _stable_hash(metadata.get("workspaceBasePath") or metadata.get("workspacePath")),
            "callIndex": job.metadata.get("callIndex", 0),
            "promptHash": _stable_hash(job.prompt),
            "optionsHash": _stable_hash(options),
            "permissionProfile": job.metadata.get("permissionProfile") or self.run.permission_profile,
            "permissionPolicyVersion": job.metadata.get("permissionPolicyVersion") or self.run.permission_policy_version,
            "toolContextHash": _stable_hash(metadata.get("toolContext")),
            "mcpContextHash": _stable_hash(metadata.get("mcpContext")),
        }

    def _append(self, event_type: str, job: WorkflowJob | None = None, payload: dict | None = None) -> None:
        self.store.append_event(
            self.run,
            WorkflowEvent(
                run_id=self.run.run_id,
                session_id=self.run.session_id,
                job_id=job.job_id if job else None,
                event_type=event_type,
                sequence=0,
                payload=payload or {},
            ),
        )


def _absolute_under(base: str | None, ref: Any) -> str | None:
    """Resolve a run-internal ref against the run's artifact directory.

    The ref alone is ambiguous -- it is relative to the run record, not to the
    run workspace -- so callers get the fully resolved path instead of a value
    they have to join onto a guessed root.
    """
    if not base or not ref:
        return None
    return str(Path(base) / str(ref))

def _stable_hash(value) -> str:
    data = json.dumps(_hashable_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(data.encode("utf-8")).hexdigest()


def _hashable_value(value):
    if value is None:
        return {"__gaWorkflowType": "none", "value": None}
    if isinstance(value, bool):
        return {"__gaWorkflowType": "bool", "value": value}
    if isinstance(value, str):
        return {"__gaWorkflowType": "str", "value": value}
    if isinstance(value, int) and not isinstance(value, bool):
        return {"__gaWorkflowType": "int", "value": value}
    if isinstance(value, float):
        return {"__gaWorkflowType": "float", "value": value}
    if isinstance(value, list):
        return {"__gaWorkflowType": "list", "value": [_hashable_value(item) for item in value]}
    if isinstance(value, tuple):
        return {"__gaWorkflowType": "tuple", "value": [_hashable_value(item) for item in value]}
    if isinstance(value, dict):
        return {
            "__gaWorkflowType": "dict",
            "value": [[_hashable_value(key), _hashable_value(value[key])] for key in sorted(value.keys(), key=lambda item: json.dumps(_hashable_value(item), ensure_ascii=False, sort_keys=True, separators=(",", ":")))],
        }
    return {"__gaWorkflowType": type(value).__name__, "value": copy.deepcopy(value)}
