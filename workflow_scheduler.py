from __future__ import annotations

import copy
import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path

from sensitive_redaction import redact_sensitive_text, sanitize
from workflow_child_agent import AgentResult, ChildAgentRunner, FakeChildAgentRunner
from workflow_models import WorkflowEvent, WorkflowJob, WorkflowRun, refresh_workflow_execution_metadata
from workflow_store import WorkflowStore


SCHEMA_VALIDATION_FAILED = "schema_validation_failed"


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
    ):
        self.store = store
        self.run = run
        self.runner = runner or FakeChildAgentRunner()
        self.config = config or SchedulerConfig()
        self.manage_run_completion = bool(manage_run_completion)
        self.args = args
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
        max_allowed, reason = self._agent_limit()
        if len(self.jobs) >= max_allowed:
            self._append("agent_rejected", payload={"reason": reason, "maxTotal": max_allowed})
            raise RuntimeError("workflow agent limit exceeded")
        workspace_path = self._sync_workspace_metadata()
        call_index = len(self.jobs)
        metadata = {
            "callIndex": call_index,
            "label": label,
            "options": options,
            "runId": self.run.run_id,
            "permissionProfile": self.run.permission_profile,
            "permissionPolicyVersion": self.run.permission_policy_version,
        }
        metadata["retryPolicy"] = normalize_retry_policy(metadata["options"].get("retryPolicy"))
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
        max_allowed, reason = self._agent_limit()
        if len(self.jobs) >= max_allowed:
            self._append("agent_rejected", payload={"reason": reason, "maxTotal": max_allowed})
            raise RuntimeError("workflow agent limit exceeded")
        workspace_path = self._sync_workspace_metadata()
        call_index = len(self.jobs)
        metadata = {
            "callIndex": call_index,
            "label": label,
            "options": options,
            "runId": self.run.run_id,
            "permissionProfile": self.run.permission_profile,
            "permissionPolicyVersion": self.run.permission_policy_version,
            "result": sanitize(result.payload),
            "cachedFromRunId": source_run_id,
            "cachedFromJobId": source_job_id,
        }
        metadata["retryPolicy"] = normalize_retry_policy(metadata["options"].get("retryPolicy"))
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
        self.workspace_path = workspace_path
        if workspace_path:
            metadata = dict(self.run.metadata) if isinstance(self.run.metadata, dict) else {}
            metadata["workspacePath"] = workspace_path
            self.run.metadata = metadata
            self.store.save_run(self.run)
        return workspace_path

    def _agent_limit(self) -> tuple[int, str]:
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

    def _start_queued_jobs(self) -> None:
        if self._stopping:
            return
        slots = self.config.max_concurrent - self.running_count
        for job in self.jobs:
            if slots <= 0:
                return
            if job.status != "queued":
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
            self.runner.start(job)
            self._append("agent_started", job)
            slots -= 1
        self.store.save_run(self.run)

    def _update_run_completion_state(self) -> None:
        if self.run.status != "running" or not self.jobs:
            return
        refresh_workflow_execution_metadata(self.run)
        if all(job.status in {"succeeded", "cached"} for job in self.jobs):
            metadata = dict(self.run.metadata) if isinstance(self.run.metadata, dict) else {}
            metadata.setdefault("integrationStatus", "pending")
            metadata.setdefault("finalAuditStatus", "pending")
            self.run.metadata = metadata
            self.run.status = "succeeded"
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

    def _complete_job(self, job: WorkflowJob, result: AgentResult) -> None:
        job.status = "succeeded"
        job.error = None
        if result.transcript_events:
            transcript_ref = self.store.write_agent_transcript(self.run, job, result.transcript_events)
            result.transcript_ref = result.transcript_ref or transcript_ref
        self.store.write_agent_result(self.run, job, result)
        handoff = self._build_handoff(result)
        job.metadata["result"] = result.payload
        job.metadata["handoff"] = handoff
        if result.transcript_ref:
            job.metadata["transcriptRef"] = result.transcript_ref
        if result.token_usage:
            job.metadata["tokenUsage"] = result.token_usage
        if result.tool_summary is not None:
            job.metadata["toolSummary"] = result.tool_summary
        self._append_permission_events_from_result(job, result)
        self._append("agent_completed", job, {"resultRef": job.result_ref, "result": self._event_result_summary(result)})

    def _build_handoff(self, result: AgentResult, *, error: str | None = None) -> dict:
        payload = result.payload if isinstance(result.payload, dict) else {}
        summary = payload.get("summary") or payload.get("text") or payload.get("error") or error or ""
        evidence = payload.get("evidence")
        if not isinstance(evidence, list):
            evidence = []
        if result.transcript_ref and not evidence:
            evidence = [{"transcriptRef": result.transcript_ref}]
        blocking = payload.get("blockingIssues")
        if not isinstance(blocking, list):
            blocking = []
        return {
            "status": result.status,
            "summary": redact_sensitive_text(str(summary))[:2_000],
            "evidence": sanitize(copy.deepcopy(evidence)),
            "blockingIssues": sanitize(copy.deepcopy(blocking)),
            "transcriptRef": result.transcript_ref,
        }

    def _schedule_retry(self, job: WorkflowJob, error: str, *, result: AgentResult | None = None) -> bool:
        policy = normalize_retry_policy(job.metadata.get("retryPolicy"))
        text_parts = [str(error or "").lower()]
        if result is not None and isinstance(result.payload, dict):
            text_parts.extend(str(result.payload.get(key) or "").lower() for key in ("code", "category", "providerAnomaly"))
        text = " ".join(text_parts)
        retryable = any(pattern and pattern in text for pattern in policy["retryableErrors"])
        if not retryable or policy["attempts"] >= policy["maxAttempts"]:
            return False
        policy["lastError"] = redact_sensitive_text(error)
        policy["retryNotBefore"] = time.time() + (policy["backoffMs"] / 1000.0)
        job.metadata["retryPolicy"] = policy
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
            return result
        error = f"{SCHEMA_VALIDATION_FAILED}: " + "; ".join(issues)
        result.status = "failed"
        result.payload = {
            "error": error,
            "code": SCHEMA_VALIDATION_FAILED,
            "schemaValidation": copy.deepcopy(validation),
        }
        return result

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
            job.metadata["handoff"] = self._build_handoff(result, error=error)
            if result.transcript_ref:
                job.metadata["transcriptRef"] = result.transcript_ref
            if result.token_usage:
                job.metadata["tokenUsage"] = result.token_usage
            if result.tool_summary is not None:
                job.metadata["toolSummary"] = result.tool_summary
            payload["resultRef"] = job.result_ref
            payload["result"] = self._event_result_summary(result)
            self._append_permission_events_from_result(job, result)
        self._append("agent_failed", job, payload)

    def _append_permission_events_from_result(self, job: WorkflowJob, result: AgentResult) -> None:
        for event in result.transcript_events:
            if event.get("type") not in {"permission_profile_selected", "tool_allowed", "tool_denied"}:
                continue
            self.store.append_permission_event(self.run, {**event, "jobId": event.get("jobId") or job.job_id})

    def _cancel_job(self, job: WorkflowJob, *, reason: str) -> None:
        if job.status in {"succeeded", "failed", "cancelled", "cached", "skipped"}:
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
            "argsHash": _stable_hash(self.args),
            "callIndex": job.metadata.get("callIndex", 0),
            "promptHash": _stable_hash(job.prompt),
            "optionsHash": _stable_hash(options),
            "permissionProfile": self.run.permission_profile,
            "permissionPolicyVersion": self.run.permission_policy_version,
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
