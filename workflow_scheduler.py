from __future__ import annotations

import copy
import hashlib
import json
from dataclasses import dataclass

from workflow_child_agent import AgentResult, ChildAgentRunner, FakeChildAgentRunner
from workflow_models import WorkflowEvent, WorkflowJob, WorkflowRun
from workflow_store import WorkflowStore


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
        self.jobs = self.run.jobs
        self._stopping = False

    @property
    def running_count(self) -> int:
        return sum(1 for job in self.jobs if job.status == "running")

    @property
    def queued_count(self) -> int:
        return sum(1 for job in self.jobs if job.status == "queued")

    def register_agent(self, *, prompt: str, label: str | None = None, options: dict | None = None) -> WorkflowJob:
        if len(self.jobs) >= self.config.max_total:
            self._append("agent_rejected", payload={"reason": "max_total_exceeded", "maxTotal": self.config.max_total})
            raise RuntimeError("workflow agent limit exceeded")
        call_index = len(self.jobs)
        job = WorkflowJob(
            job_id=f"agent_{call_index + 1}",
            prompt=prompt,
            status="queued",
            metadata={
                "callIndex": call_index,
                "label": label,
                "options": normalize_agent_options(options),
                "runId": self.run.run_id,
                "permissionProfile": self.run.permission_profile,
                "permissionPolicyVersion": self.run.permission_policy_version,
            },
        )
        job.metadata["cacheKey"] = self._cache_key(job)
        self.jobs.append(job)
        self.store.save_run(self.run)
        self._append("agent_registered", job, {"cacheKey": job.metadata["cacheKey"], "label": label})
        return job

    def register_cached_agent(self, *, prompt: str, label: str | None = None, options: dict | None = None, result: AgentResult, source_run_id: str | None = None, source_job_id: str | None = None) -> WorkflowJob:
        if len(self.jobs) >= self.config.max_total:
            self._append("agent_rejected", payload={"reason": "max_total_exceeded", "maxTotal": self.config.max_total})
            raise RuntimeError("workflow agent limit exceeded")
        call_index = len(self.jobs)
        job = WorkflowJob(
            job_id=f"agent_{call_index + 1}",
            prompt=prompt,
            status="cached",
            metadata={
                "callIndex": call_index,
                "label": label,
                "options": normalize_agent_options(options),
                "runId": self.run.run_id,
                "permissionProfile": self.run.permission_profile,
                "permissionPolicyVersion": self.run.permission_policy_version,
                "result": result.payload,
                "cachedFromRunId": source_run_id,
                "cachedFromJobId": source_job_id,
            },
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
        return job

    def tick(self, *, failure_policy: str = "continue") -> list[WorkflowJob]:
        completed: list[WorkflowJob] = []
        self._start_queued_jobs()
        for job in list(self.jobs):
            if job.status != "running":
                continue
            try:
                result = self.runner.poll(job)
            except Exception as exc:
                self._fail_job(job, str(exc))
                completed.append(job)
                if failure_policy == "fail_fast":
                    self._fail_fast(str(exc))
                continue
            if result is None:
                continue
            if result.status == "cancelled":
                self._cancel_job(job, reason="cancelled")
            elif result.status == "failed":
                self._fail_job(job, str(result.payload.get("error") or "child agent failed"), result=result)
                if failure_policy == "fail_fast":
                    self._fail_fast(job.error or "child agent failed")
            else:
                self._complete_job(job, result)
            completed.append(job)
        if self.manage_run_completion:
            self._update_run_completion_state()
        self.store.save_run(self.run)
        return completed

    def run_all(self, *, failure_policy: str = "continue") -> list[WorkflowJob]:
        completed: list[WorkflowJob] = []
        while any(job.status in {"queued", "running"} for job in self.jobs):
            before = [(job.job_id, job.status) for job in self.jobs]
            completed.extend(self.tick(failure_policy=failure_policy))
            after = [(job.job_id, job.status) for job in self.jobs]
            if before == after and any(job.status == "running" for job in self.jobs):
                continue
        if self.manage_run_completion:
            self._update_run_completion_state()
            self.store.save_run(self.run)
        return completed

    def stop(self, *, reason: str = "") -> None:
        self._stopping = True
        self.run.status = "killed"
        self.run.error = reason or None
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
            job.status = "running"
            self.runner.start(job)
            self._append("agent_started", job)
            slots -= 1
        self.store.save_run(self.run)

    def _update_run_completion_state(self) -> None:
        if self.run.status != "running" or not self.jobs:
            return
        if all(job.status in {"succeeded", "cached"} for job in self.jobs):
            self.run.status = "succeeded"
            self.store.write_final_result(
                self.run,
                {
                    "runId": self.run.run_id,
                    "status": self.run.status,
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
        job.metadata["result"] = result.payload
        if result.transcript_ref:
            job.metadata["transcriptRef"] = result.transcript_ref
        if result.token_usage:
            job.metadata["tokenUsage"] = result.token_usage
        if result.tool_summary is not None:
            job.metadata["toolSummary"] = result.tool_summary
        self._append_permission_events_from_result(job, result)
        self._append("agent_completed", job, {"resultRef": job.result_ref, "result": result.to_artifact_dict()})

    def _fail_job(self, job: WorkflowJob, error: str, result: AgentResult | None = None) -> None:
        job.status = "failed"
        job.error = error
        payload = {"error": error}
        if result is not None:
            if result.transcript_events:
                transcript_ref = self.store.write_agent_transcript(self.run, job, result.transcript_events)
                result.transcript_ref = result.transcript_ref or transcript_ref
            self.store.write_agent_result(self.run, job, result)
            if result.transcript_ref:
                job.metadata["transcriptRef"] = result.transcript_ref
            if result.token_usage:
                job.metadata["tokenUsage"] = result.token_usage
            if result.tool_summary is not None:
                job.metadata["toolSummary"] = result.tool_summary
            payload["resultRef"] = job.result_ref
            payload["result"] = result.to_artifact_dict()
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
        self._append("agent_cancelled", job, {"reason": reason or ""})

    def _fail_fast(self, error: str) -> None:
        self.run.status = "failed"
        self.run.error = error
        for job in list(self.jobs):
            if job.status == "queued":
                self._cancel_job(job, reason="fail_fast")
            elif job.status == "running":
                self.runner.cancel(job)
                self._cancel_job(job, reason="fail_fast")

    def _cache_key(self, job: WorkflowJob) -> dict:
        options = job.metadata.get("options") or {}
        return {
            "scriptHash": _stable_hash(self.run.script),
            "argsHash": _stable_hash(self.args),
            "callIndex": job.metadata.get("callIndex", 0),
            "promptHash": _stable_hash(job.prompt),
            "optionsHash": _stable_hash(options),
            "permissionProfile": self.run.permission_profile,
            "permissionPolicyVersion": self.run.permission_policy_version,
        }

    def _append(self, event_type: str, job: WorkflowJob | None = None, payload: dict | None = None) -> None:
        self.store.append_event(
            self.run,
            WorkflowEvent(
                run_id=self.run.run_id,
                session_id=self.run.session_id,
                job_id=job.job_id if job else None,
                event_type=event_type,
                sequence=self._next_sequence(),
                payload=payload or {},
            ),
        )

    def _next_sequence(self) -> int:
        return max((event.sequence for event in self.store.replay_events(self.run.run_id)), default=0) + 1


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
