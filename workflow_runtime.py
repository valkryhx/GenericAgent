from __future__ import annotations

import json
import copy
import queue
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sensitive_redaction import redact_sensitive_text, sanitize
from workflow_child_agent import AgentResult, FakeChildAgentRunner, NativeGPTChildAgentRunner
from workflow_models import WorkflowEvent, WorkflowJob, WorkflowRun
from workflow_scheduler import AgentScheduler, SchedulerConfig
from workflow_store import WorkflowStore


FORBIDDEN_SCRIPT_TOKENS = (
    "require",
    "import",
    "process",
    "fs",
    "child_process",
    "fetch",
    "XMLHttpRequest",
    "Deno",
    "Bun",
    "WebSocket",
)


@dataclass
class WorkflowRuntimeResult:
    run: WorkflowRun
    result: Any = None
    logs: list[str] = field(default_factory=list)
    phases: list[str] = field(default_factory=list)


class WorkflowRuntime:
    def __init__(
        self,
        *,
        store: WorkflowStore | None = None,
        runner=None,
        scheduler_config: SchedulerConfig | None = None,
        worker_path: str | Path | None = None,
        timeout_seconds: float = 10.0,
        llm_binding_provider=None,
    ):
        self.store = store or WorkflowStore()
        self.llm_binding_provider = llm_binding_provider
        # Production default: real child via llm.yaml (or binding_provider).
        # Unit tests must pass runner=FakeChildAgentRunner() explicitly.
        if runner is not None:
            self.runner = runner
        else:
            self.runner = self._default_runner()
        self.scheduler_config = scheduler_config or SchedulerConfig()
        self.worker_path = Path(worker_path) if worker_path else Path(__file__).resolve().with_name("workflow_js_worker.js")
        self.timeout_seconds = float(timeout_seconds)
        self._logs: list[str] = []
        self._phases: list[str] = []

    def _default_runner(self):
        kwargs = {"enable_tools": True}
        if self.llm_binding_provider is not None:
            kwargs["binding_provider"] = self.llm_binding_provider
        return NativeGPTChildAgentRunner(**kwargs)

    def run(self, run: WorkflowRun, *, args: Any = None, resume_from_run_id: str | None = None) -> WorkflowRuntimeResult:
        self._scan_script(run.script)
        self._logs = []
        self._phases = []
        # Record LLM binding snapshot for audit (best-effort; no secrets).
        try:
            from workflow_llm import binding_from_env, resolve_binding

            if self.llm_binding_provider is not None:
                binding = resolve_binding(binding_provider=self.llm_binding_provider)
            elif hasattr(self.runner, "binding_provider") and getattr(self.runner, "binding_provider", None):
                binding = resolve_binding(binding_provider=self.runner.binding_provider)
            elif hasattr(self.runner, "profile_name") and getattr(self.runner, "profile_name", None):
                binding = resolve_binding(profile_name=self.runner.profile_name)
            elif isinstance(self.runner, FakeChildAgentRunner):
                binding = None
            else:
                binding = binding_from_env()
            if binding is not None:
                meta = run.metadata if isinstance(run.metadata, dict) else {}
                meta = dict(meta)
                meta.update(binding.as_metadata())
                run.metadata = meta
        except Exception:
            pass
        if not run.artifact_dir:
            run = self.store.create_run(run)
        if run.status in {"draft", "awaiting_approval"}:
            run.status = "running"
            self.store.save_run(run)
        resume_plan = self._build_resume_plan(run, args=args, resume_from_run_id=resume_from_run_id)
        scheduler = AgentScheduler(
            store=self.store,
            run=run,
            runner=self.runner,
            config=self.scheduler_config,
            manage_run_completion=False,
            args=args,
        )
        process = subprocess.Popen(
            [self._node_executable(), str(self.worker_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        reader_queue, reader_done = self._start_reader(process)
        deadline = time.monotonic() + self.timeout_seconds
        pending_rpc_jobs: dict[int, WorkflowJob] = {}
        try:
            ready = self._wait_for_message(process, reader_queue, reader_done, deadline)
            if ready.get("type") != "ready":
                raise RuntimeError(f"workflow worker did not become ready: {ready}")
            timeout_ms = max(1, int(self.timeout_seconds * 1000))
            self._send(process, {"type": "start", "script": run.script, "args": args, "timeoutMs": timeout_ms})
            while True:
                self._raise_if_deadline_expired(deadline)
                self._raise_if_externally_killed(run, scheduler, process)

                for completed_job in scheduler.tick(failure_policy="continue"):
                    self._complete_pending_rpc(process, pending_rpc_jobs, completed_job)

                message = self._next_message(process, reader_queue, reader_done, deadline)
                if message is None:
                    continue
                message_type = message.get("type")
                if message_type == "rpc":
                    self._handle_rpc(scheduler, message, pending_rpc_jobs, resume_plan=resume_plan, process=process)
                elif message_type == "event":
                    self._handle_worker_event(run, message)
                elif message_type == "done":
                    result = sanitize(message.get("result"))
                    run.status = "succeeded"
                    run.error = None
                    self.store.save_run(run)
                    self.store.write_workflow_progress(run)
                    final_payload = self._final_payload(run, "succeeded", result=result)
                    self.store.write_final_result(run, final_payload)
                    return WorkflowRuntimeResult(run=run, result=result, logs=list(self._logs), phases=list(self._phases))
                elif message_type == "error":
                    raise RuntimeError(redact_sensitive_text(message.get("error") or "workflow worker failed"))
        except Exception as exc:
            reason = redact_sensitive_text(str(exc))
            self._cancel_unfinished_jobs(scheduler, reason=reason)
            current = self._safe_load_current_run(run)
            if current.status == "killed":
                run.status = "killed"
                run.error = current.error or reason
                self.store.save_run(run)
                self.store.write_workflow_progress(run)
                self.store.write_final_result(run, self._final_payload(run, "killed", error=run.error))
                self._append(run, "workflow_killed", {"error": run.error})
            else:
                run.status = "failed"
                run.error = reason
                self.store.save_run(run)
                self.store.write_workflow_progress(run)
                self.store.write_final_result(run, self._final_payload(run, "failed", error=reason))
                self._append(run, "workflow_failed", {"error": reason})
            raise
        finally:
            self._terminate(process)
            reader_done.set()

    def _handle_rpc(
        self,
        scheduler: AgentScheduler,
        message: dict,
        pending_rpc_jobs: dict[int, WorkflowJob],
        *,
        resume_plan: list[dict] | None = None,
        process: subprocess.Popen | None = None,
    ) -> None:
        method = message.get("method")
        params = message.get("params") or {}
        if method != "agent":
            raise RuntimeError(f"unsupported workflow rpc: {method}")
        options = params.get("options") or {}
        if not isinstance(options, dict):
            raise TypeError("agent options must be a plain object")
        label = options.get("label")
        if label is not None and not isinstance(label, str):
            raise TypeError("agent option label must be a string")
        prompt = str(params.get("prompt") or "")
        call_index = len(scheduler.jobs)
        cached = self._match_cached_agent(resume_plan, call_index=call_index, prompt=prompt, options=options, scheduler=scheduler)
        if cached is not None:
            job = scheduler.register_cached_agent(
                prompt=prompt,
                label=label,
                options=options,
                result=cached["result"],
                source_run_id=cached.get("sourceRunId"),
                source_job_id=cached.get("sourceJobId"),
            )
            if process is not None:
                self._send(process, {"type": "rpc_result", "id": int(message.get("id")), "ok": True, "value": job.metadata.get("result") or {}})
            return
        job = scheduler.register_agent(prompt=prompt, label=label, options=options)
        pending_rpc_jobs[int(message.get("id"))] = job

    def _complete_pending_rpc(self, process: subprocess.Popen, pending_rpc_jobs: dict[int, WorkflowJob], job: WorkflowJob) -> None:
        rpc_id = None
        for candidate_id, candidate_job in pending_rpc_jobs.items():
            if candidate_job.job_id == job.job_id:
                rpc_id = candidate_id
                break
        if rpc_id is None:
            return
        pending_rpc_jobs.pop(rpc_id, None)
        if job.status == "succeeded":
            self._send(process, {"type": "rpc_result", "id": rpc_id, "ok": True, "value": job.metadata.get("result") or {}})
        else:
            self._send(process, {"type": "rpc_result", "id": rpc_id, "ok": False, "error": redact_sensitive_text(job.error or f"workflow agent failed: {job.job_id}")})

    def _handle_worker_event(self, run: WorkflowRun, message: dict) -> None:
        method = message.get("method")
        params = message.get("params") or {}
        if method == "phase":
            name = str(params.get("name") or "")
            self._phases.append(name)
            self._append(run, "workflow_phase", {"name": name})
        elif method == "log":
            text = redact_sensitive_text(str(params.get("message") or ""))
            self._logs.append(text)
            self._append(run, "workflow_log", {"message": text})

    def _build_resume_plan(self, run: WorkflowRun, *, args: Any = None, resume_from_run_id: str | None = None) -> list[dict]:
        if not resume_from_run_id or resume_from_run_id == run.run_id:
            return []
        try:
            source_run = self.store.load_run(resume_from_run_id)
        except Exception:
            return []
        if source_run.session_id != run.session_id:
            return []
        plan: list[dict] = []
        probe_scheduler = AgentScheduler(store=self.store, run=run, runner=self.runner, config=self.scheduler_config, manage_run_completion=False, args=args)
        for source_job in source_run.jobs:
            if source_job.status not in {"succeeded", "cached"}:
                break
            source_key = source_job.metadata.get("cacheKey") or {}
            expected_key = probe_scheduler._cache_key(
                WorkflowJob(job_id="probe", prompt=source_job.prompt, metadata={"callIndex": source_job.metadata.get("callIndex", len(plan)), "options": source_job.metadata.get("options") or {}})
            )
            for field in (
                "argsHash",
                "permissionProfile",
                "permissionPolicyVersion",
                "toolContextHash",
                "mcpContextHash",
            ):
                if source_key.get(field) != expected_key.get(field):
                    return plan
            try:
                result = self.store.read_agent_result(source_run, source_job)
            except Exception:
                break
            plan.append(
                {
                    "callIndex": source_job.metadata.get("callIndex", len(plan)),
                    "prompt": source_job.prompt,
                    "options": source_job.metadata.get("options") or {},
                    "promptHash": source_key.get("promptHash"),
                    "optionsHash": source_key.get("optionsHash"),
                    "result": result,
                    "sourceRunId": source_run.run_id,
                    "sourceJobId": source_job.job_id,
                }
            )
        return plan

    def _match_cached_agent(
        self,
        resume_plan: list[dict] | None,
        *,
        call_index: int,
        prompt: str,
        options: dict,
        scheduler: AgentScheduler,
    ) -> dict | None:
        if not resume_plan or call_index >= len(resume_plan):
            return None
        candidate = resume_plan[call_index]
        probe = WorkflowJob(job_id="probe", prompt=prompt, metadata={"callIndex": call_index, "options": dict(options or {})})
        key = scheduler._cache_key(probe)
        if candidate.get("callIndex") != call_index:
            return None
        if candidate.get("promptHash") != key.get("promptHash"):
            del resume_plan[call_index:]
            return None
        if candidate.get("optionsHash") != key.get("optionsHash"):
            del resume_plan[call_index:]
            return None
        return candidate

    def _scan_script(self, script: str) -> None:
        for token in FORBIDDEN_SCRIPT_TOKENS:
            if re.search(rf"\b{re.escape(token)}\b", script):
                raise ValueError(f"workflow script uses forbidden token: {token}")

    def _append(self, run: WorkflowRun, event_type: str, payload: dict | None = None) -> None:
        self.store.append_event(
            run,
            WorkflowEvent(
                run_id=run.run_id,
                session_id=run.session_id,
                event_type=event_type,
                sequence=max((event.sequence for event in self.store.replay_events(run.run_id)), default=0) + 1,
                payload=payload or {},
            ),
        )

    def _start_reader(self, process: subprocess.Popen) -> tuple[queue.Queue, threading.Event]:
        if process.stdout is None:
            raise RuntimeError("workflow worker stdout unavailable")
        messages: queue.Queue = queue.Queue()
        done = threading.Event()

        def reader() -> None:
            try:
                for line in process.stdout:
                    messages.put(line)
            finally:
                done.set()

        thread = threading.Thread(target=reader, daemon=True)
        thread.start()
        return messages, done

    def _wait_for_message(self, process: subprocess.Popen, messages: queue.Queue, done: threading.Event, deadline: float) -> dict:
        while True:
            self._raise_if_deadline_expired(deadline)
            message = self._next_message(process, messages, done, deadline)
            if message is not None:
                return message

    def _next_message(self, process: subprocess.Popen, messages: queue.Queue, done: threading.Event, deadline: float) -> dict | None:
        timeout = min(0.02, max(0.0, deadline - time.monotonic()))
        try:
            line = messages.get(timeout=timeout)
        except queue.Empty:
            if process.poll() is not None and done.is_set():
                stderr = process.stderr.read() if process.stderr else ""
                raise RuntimeError(f"workflow worker exited unexpectedly: {redact_sensitive_text(stderr.strip())}")
            return None
        if not line:
            return None
        return json.loads(line)

    def _read_message(self, process: subprocess.Popen) -> dict:
        if process.stdout is None:
            raise RuntimeError("workflow worker stdout unavailable")
        line = process.stdout.readline()
        if not line:
            stderr = process.stderr.read() if process.stderr else ""
            raise RuntimeError(f"workflow worker exited unexpectedly: {stderr.strip()}")
        return json.loads(line)

    def _send(self, process: subprocess.Popen, message: dict) -> None:
        if process.stdin is None:
            raise RuntimeError("workflow worker stdin unavailable")
        process.stdin.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
        process.stdin.flush()

    def _raise_if_deadline_expired(self, deadline: float) -> None:
        if time.monotonic() >= deadline:
            raise RuntimeError("workflow runtime deadline exceeded")

    def _raise_if_externally_killed(self, run: WorkflowRun, scheduler: AgentScheduler, process: subprocess.Popen) -> None:
        current = self._safe_load_current_run(run)
        if current.status != "killed":
            return
        run.status = "killed"
        run.error = current.error or "workflow killed"
        self._cancel_unfinished_jobs(scheduler, reason=run.error)
        if process.poll() is None:
            process.kill()
            try:
                process.wait(timeout=1)
            except Exception:
                pass
        raise RuntimeError(f"workflow killed: {run.error}")

    def _safe_load_current_run(self, run: WorkflowRun) -> WorkflowRun:
        try:
            return self.store.load_run(run.run_id)
        except Exception:
            return run

    def _cancel_unfinished_jobs(self, scheduler: AgentScheduler, *, reason: str) -> None:
        for job in list(scheduler.jobs):
            if job.status == "queued":
                scheduler._cancel_job(job, reason=reason)
            elif job.status == "running":
                scheduler.runner.cancel(job)
                scheduler._cancel_job(job, reason=reason)
        scheduler.store.save_run(scheduler.run)

    def _final_payload(self, run: WorkflowRun, status: str, *, result: Any = None, error: str | None = None) -> dict:
        payload: dict[str, Any] = {
            "runId": run.run_id,
            "status": status,
            "workflowProgressRef": "workflow-progress.json",
            "workflowIssues": sanitize(copy.deepcopy((run.metadata or {}).get("workflowIssues") or [])),
            "jobs": [
                {
                    "jobId": job.job_id,
                    "status": job.status,
                    "resultRef": job.result_ref,
                    "error": job.error,
                }
                for job in run.jobs
            ],
        }
        if result is not None:
            payload["result"] = sanitize(result)
        if error is not None:
            payload["error"] = redact_sensitive_text(error)
        return sanitize(payload)

    def _terminate(self, process: subprocess.Popen) -> None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
                try:
                    process.wait(timeout=1)
                except Exception:
                    pass
        for stream in (process.stdin, process.stdout, process.stderr):
            try:
                if stream is not None and not stream.closed:
                    stream.close()
            except Exception:
                pass

    @staticmethod
    def _node_executable() -> str:
        return "node.exe" if sys.platform.startswith("win") else "node"
