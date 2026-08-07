from __future__ import annotations

import json
import copy
import os
import queue
import re
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sensitive_redaction import is_sensitive_key, redact_sensitive_text, sanitize
from workflow_child_agent import AgentResult, FakeChildAgentRunner, NativeGPTChildAgentRunner
from workflow_models import WorkflowEvent, WorkflowJob, WorkflowRun, refresh_workflow_execution_metadata
from workflow_scheduler import AgentScheduler, SchedulerConfig, normalize_workflow_workspace
from workflow_store import WorkflowStore


MAX_TEST_GATE_TIMEOUT_MS = 120_000
MAX_TEST_OUTPUT_CHARS = 12_000
TEST_GATE_FIELDS = frozenset(
    {
        "workspacePath",
        "workspace",
        "startDir",
        "pattern",
        "topLevelDir",
        "timeoutMs",
        "expect",
        "phase",
        "gateKey",
    }
)
SENSITIVE_TEST_FILENAMES = frozenset({"mykey.py", "mykey.json", "mcp.json"})


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
        self._test_gates: list[dict] = []
        self._last_worker_result: Any = None

    def _default_runner(self):
        kwargs = {"enable_tools": True}
        if self.llm_binding_provider is not None:
            kwargs["binding_provider"] = self.llm_binding_provider
        return NativeGPTChildAgentRunner(**kwargs)

    def run(self, run: WorkflowRun, *, args: Any = None, resume_from_run_id: str | None = None) -> WorkflowRuntimeResult:
        self._logs = []
        self._phases = []
        self._test_gates = []
        self._last_worker_result = None
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
        workspace_path = normalize_workflow_workspace(args)
        if workspace_path:
            meta = run.metadata if isinstance(run.metadata, dict) else {}
            meta = dict(meta)
            meta["workspacePath"] = workspace_path
            run.metadata = meta
        if not run.artifact_dir:
            run = self.store.create_run(run)
        if run.status in {"draft", "awaiting_approval"}:
            run.status = "running"
            self.store.save_run(run)
        elif workspace_path:
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
                    self._handle_rpc(
                        scheduler,
                        message,
                        pending_rpc_jobs,
                        resume_plan=resume_plan,
                        process=process,
                        args=args,
                        deadline=deadline,
                    )
                elif message_type == "event":
                    self._handle_worker_event(run, message)
                elif message_type == "done":
                    result = sanitize(message.get("result"))
                    self._last_worker_result = result
                    gate_error = self._test_gate_failure_reason()
                    verification_error = self._explicit_verification_failure_reason(result)
                    if gate_error or verification_error:
                        raise RuntimeError(gate_error or verification_error)
                    run.status = "succeeded"
                    run.error = None
                    refresh_workflow_execution_metadata(run)
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
                refresh_workflow_execution_metadata(run)
                self.store.save_run(run)
                self.store.write_workflow_progress(run)
                self.store.write_final_result(
                    run,
                    self._final_payload(run, "killed", result=self._last_worker_result, error=run.error),
                )
                self._append(run, "workflow_killed", {"error": run.error})
            else:
                run.status = "failed"
                run.error = reason
                refresh_workflow_execution_metadata(run)
                self.store.save_run(run)
                self.store.write_workflow_progress(run)
                self.store.write_final_result(
                    run,
                    self._final_payload(run, "failed", result=self._last_worker_result, error=reason),
                )
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
        args: Any = None,
        deadline: float | None = None,
    ) -> None:
        method = message.get("method")
        params = message.get("params") or {}
        if method == "runPythonUnittest":
            if process is None:
                raise RuntimeError("workflow test gate process unavailable")
            result = self._run_python_unittest(
                scheduler.run,
                params,
                args=args,
                deadline=deadline or (time.monotonic() + self.timeout_seconds),
            )
            self._send(process, {"type": "rpc_result", "id": int(message.get("id")), "ok": True, "value": result})
            return
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

    def _run_python_unittest(self, run: WorkflowRun, params: dict, *, args: Any, deadline: float) -> dict:
        gate_number = len(self._test_gates) + 1
        gate_id = f"gate-{gate_number}"
        raw_phase = params.get("phase") if isinstance(params, dict) else None
        phase = str(raw_phase or "")[:64] if raw_phase is not None else ""
        expectation = self._test_gate_expectation(params)
        self._append(
            run,
            "workflow_test_gate_started",
            {"gateId": gate_id, "expectation": expectation, "phase": phase or None},
        )
        started_at = time.monotonic()
        spec = None
        try:
            spec = self._normalize_test_gate_spec(params, args=args)
            gate_key = spec.get("gateKey") or gate_id
            result = self._execute_python_unittest(spec, deadline=deadline)
        except Exception as exc:
            gate_key = gate_id
            result = {
                "passed": False,
                "returncode": None,
                "stdout": "",
                "stderr": "",
                "truncated": False,
                "timedOut": False,
                "error": redact_sensitive_text(str(exc)),
                "cwd": None,
                "commandKind": "python_unittest",
            }
        result["type"] = "python_unittest_result"
        result["gateId"] = gate_id
        result["gateKey"] = gate_key
        result["expectation"] = expectation
        result["gatePassed"] = self._gate_passed_for_expectation(result, expectation)
        result["phase"] = phase or None
        result["durationMs"] = max(0, int((time.monotonic() - started_at) * 1000))
        result["stdout"] = redact_sensitive_text(str(result.get("stdout") or ""))
        result["stderr"] = redact_sensitive_text(str(result.get("stderr") or ""))
        result["error"] = redact_sensitive_text(str(result.get("error") or "")) or None
        result = sanitize(result)

        artifact_ref = self.store.write_test_gate_result(run, gate_id, result)
        result["artifactRef"] = artifact_ref
        failure_ref = None
        workspace_failure_ref = None
        if not result.get("passed"):
            failure_text = result.get("stderr") or result.get("stdout") or result.get("error") or "python unittest failed"
            failure_ref = self.store.write_test_failures(run, failure_text)
            if spec is not None:
                workspace_failure_ref = self.store.write_test_failures_to_workspace(spec["workspace"], failure_text)
            result["failureRef"] = failure_ref
            result["workspaceFailureRef"] = workspace_failure_ref
        self.store.write_test_gate_result(run, gate_id, result)

        self._test_gates.append(result)
        metadata = dict(run.metadata) if isinstance(run.metadata, dict) else {}
        gate_summaries = list(metadata.get("testGates") or [])
        gate_summaries.append(
            {
                "gateId": gate_id,
                "gateKey": gate_key,
                "phase": phase or None,
                "expectation": expectation,
                "passed": bool(result.get("passed")),
                "gatePassed": bool(result.get("gatePassed")),
                "artifactRef": artifact_ref,
                "failureRef": failure_ref,
                "workspaceFailureRef": workspace_failure_ref,
            }
        )
        metadata["testGates"] = gate_summaries
        run.metadata = metadata
        self.store.save_run(run)
        self.store.write_workflow_progress(run)
        self._append(
            run,
            "workflow_test_gate_completed",
            {
                "gateId": gate_id,
                "gateKey": gate_key,
                "expectation": expectation,
                "passed": bool(result.get("passed")),
                "gatePassed": bool(result.get("gatePassed")),
                "artifactRef": artifact_ref,
                "failureRef": failure_ref,
                "workspaceFailureRef": workspace_failure_ref,
            },
        )
        if not result.get("gatePassed"):
            self._append(
                run,
                "workflow_test_gate_failed",
                {
                    "gateId": gate_id,
                    "gateKey": gate_key,
                    "artifactRef": artifact_ref,
                    "failureRef": failure_ref,
                    "workspaceFailureRef": workspace_failure_ref,
                    "error": self._test_gate_failure_preview(result),
                },
            )
        return result

    def _normalize_test_gate_spec(self, params: dict, *, args: Any) -> dict:
        if not isinstance(params, dict):
            raise TypeError("runPythonUnittest params must be a plain object")
        unknown = sorted(set(params) - TEST_GATE_FIELDS)
        if unknown:
            raise ValueError(f"runPythonUnittest has unsupported fields: {', '.join(unknown)}")
        workspace_raw = params.get("workspacePath") or params.get("workspace")
        if not isinstance(workspace_raw, str) or not workspace_raw.strip():
            raise ValueError("runPythonUnittest requires workspacePath")
        trusted_root_raw = None
        if isinstance(args, dict):
            trusted_root_raw = args.get("workspacePath") or args.get("workspace")
        if not isinstance(trusted_root_raw, str) or not trusted_root_raw.strip():
            raise ValueError("workflow args must provide workspacePath for test gate")
        trusted_root = Path(trusted_root_raw).expanduser().resolve()
        workspace = Path(workspace_raw).expanduser().resolve()
        if not self._is_within(workspace, trusted_root):
            raise ValueError("test gate workspace is outside the allowed workflow workspace")
        if not workspace.is_dir():
            raise ValueError("test gate workspace must be an existing directory")

        start_raw = params.get("startDir", ".")
        if not isinstance(start_raw, str) or not start_raw.strip():
            raise ValueError("test gate startDir must be a non-empty relative path")
        start_dir = self._resolve_test_path(workspace, start_raw, "startDir")
        if not start_dir.is_dir():
            raise ValueError("test gate startDir must be an existing directory")
        top_raw = params.get("topLevelDir")
        top_level_dir = None
        if top_raw is not None:
            if not isinstance(top_raw, str) or not top_raw.strip():
                raise ValueError("test gate topLevelDir must be a relative path")
            top_level_dir = self._resolve_test_path(workspace, top_raw, "topLevelDir")
            if not top_level_dir.is_dir():
                raise ValueError("test gate topLevelDir must be an existing directory")

        pattern = params.get("pattern", "test_*.py")
        if not isinstance(pattern, str) or not pattern or len(pattern) > 128 or any(char in pattern for char in ("/", "\\", "\x00")):
            raise ValueError("test gate pattern must be a short filename pattern")
        timeout_ms = params.get("timeoutMs", 30_000)
        if isinstance(timeout_ms, bool) or not isinstance(timeout_ms, (int, float)):
            raise ValueError("test gate timeoutMs must be a number")
        timeout_ms = int(timeout_ms)
        if timeout_ms < 1 or timeout_ms > MAX_TEST_GATE_TIMEOUT_MS:
            raise ValueError(f"test gate timeoutMs must be between 1 and {MAX_TEST_GATE_TIMEOUT_MS}")
        gate_key = params.get("gateKey")
        if gate_key is not None:
            if not isinstance(gate_key, str) or not re.fullmatch(r"[A-Za-z0-9._-]{1,64}", gate_key):
                raise ValueError("test gate gateKey is invalid")

        for sensitive_name in SENSITIVE_TEST_FILENAMES:
            if (start_dir / sensitive_name).exists():
                raise ValueError(f"test gate refuses sensitive file: {sensitive_name}")
        return {
            "workspace": workspace,
            "startDir": start_dir,
            "startArg": start_raw,
            "topLevelDir": top_level_dir,
            "topLevelArg": top_raw,
            "pattern": pattern,
            "timeoutMs": timeout_ms,
            "expectation": self._test_gate_expectation(params),
            "gateKey": gate_key,
        }

    def _execute_python_unittest(self, spec: dict, *, deadline: float) -> dict:
        remaining = max(0.001, deadline - time.monotonic())
        timeout_seconds = min(float(spec["timeoutMs"]) / 1000.0, remaining)
        command = [sys.executable, "-m", "unittest", "discover", "-s", str(spec["startArg"]), "-p", spec["pattern"]]
        if spec.get("topLevelDir") is not None:
            command.extend(["-t", str(spec["topLevelArg"])])
        env = {key: value for key, value in os.environ.items() if not is_sensitive_key(key)}
        try:
            completed = subprocess.run(
                command,
                cwd=str(spec["workspace"]),
                env=env,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=timeout_seconds,
                check=False,
            )
            raw_stdout = self._process_output_text(completed.stdout)
            raw_stderr = self._process_output_text(completed.stderr)
            stdout, stdout_truncated = self._truncate_test_output(raw_stdout)
            stderr, stderr_truncated = self._truncate_test_output(raw_stderr)
            combined = f"{raw_stdout}\n{raw_stderr}"
            count_match = re.search(r"Ran\s+(\d+)\s+tests?", combined)
            test_count = int(count_match.group(1)) if count_match else None
            passed = completed.returncode == 0 and test_count != 0
            error = "no tests discovered" if completed.returncode == 0 and test_count == 0 else None
            return {
                "passed": passed,
                "returncode": completed.returncode,
                "stdout": stdout,
                "stderr": stderr,
                "truncated": stdout_truncated or stderr_truncated,
                "timedOut": False,
                "error": error,
                "cwd": str(spec["workspace"]),
                "commandKind": "python_unittest",
                "testCount": test_count,
            }
        except subprocess.TimeoutExpired as exc:
            stdout, stdout_truncated = self._truncate_test_output(self._process_output_text(exc.stdout))
            stderr, stderr_truncated = self._truncate_test_output(self._process_output_text(exc.stderr))
            return {
                "passed": False,
                "returncode": None,
                "stdout": stdout,
                "stderr": stderr,
                "truncated": stdout_truncated or stderr_truncated,
                "timedOut": True,
                "error": "python unittest timed out",
                "cwd": str(spec["workspace"]),
                "commandKind": "python_unittest",
                "testCount": None,
            }
        except OSError as exc:
            return {
                "passed": False,
                "returncode": None,
                "stdout": "",
                "stderr": "",
                "truncated": False,
                "timedOut": False,
                "error": redact_sensitive_text(str(exc)),
                "cwd": str(spec["workspace"]),
                "commandKind": "python_unittest",
                "testCount": None,
            }

    @staticmethod
    def _resolve_test_path(workspace: Path, raw: str, field: str) -> Path:
        candidate = Path(raw)
        if candidate.is_absolute() or "\x00" in raw or ".." in candidate.parts:
            raise ValueError(f"test gate {field} must stay within workspace")
        resolved = (workspace / candidate).resolve()
        if not WorkflowRuntime._is_within(resolved, workspace):
            raise ValueError(f"test gate {field} must stay within workspace")
        return resolved

    @staticmethod
    def _is_within(path: Path, parent: Path) -> bool:
        try:
            path.relative_to(parent)
            return True
        except ValueError:
            return False

    @staticmethod
    def _test_gate_expectation(params: dict) -> str:
        expectation = params.get("expect", "pass") if isinstance(params, dict) else "pass"
        if expectation not in {"pass", "fail"}:
            return "pass"
        return expectation

    @staticmethod
    def _gate_passed_for_expectation(result: dict, expectation: str) -> bool:
        if expectation == "pass":
            return bool(result.get("passed"))
        return (
            not bool(result.get("passed"))
            and not result.get("error")
            and not result.get("timedOut")
            and isinstance(result.get("returncode"), int)
            and result.get("returncode") != 0
            and isinstance(result.get("testCount"), int)
            and result.get("testCount") > 0
        )

    @staticmethod
    def _truncate_test_output(value: Any) -> tuple[str, bool]:
        text = WorkflowRuntime._process_output_text(value)
        if len(text) <= MAX_TEST_OUTPUT_CHARS:
            return text, False
        return text[:MAX_TEST_OUTPUT_CHARS] + "\n[output truncated]", True

    @staticmethod
    def _process_output_text(value: Any) -> str:
        if value is None:
            return ""
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)

    def _test_gate_failure_reason(self) -> str | None:
        latest_by_key = {}
        for result in self._test_gates:
            latest_by_key[result.get("gateKey") or result.get("gateId")] = result
        for result in reversed(self._test_gates):
            gate_key = result.get("gateKey") or result.get("gateId")
            if latest_by_key.get(gate_key) is not result:
                continue
            if result.get("gatePassed"):
                continue
            return f"workflow test gate failed: {result.get('gateId')}: {self._test_gate_failure_preview(result)}"
        return None

    @staticmethod
    def _explicit_verification_failure_reason(result: Any) -> str | None:
        if isinstance(result, dict) and result.get("verificationPassed") is False:
            return "workflow verification failed: verificationPassed=false"
        return None

    @staticmethod
    def _test_gate_failure_preview(result: dict) -> str:
        text = result.get("error") or result.get("stderr") or result.get("stdout") or "test gate did not pass"
        text = redact_sensitive_text(str(text)).strip()
        return text[:2_000]

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

    def _append(self, run: WorkflowRun, event_type: str, payload: dict | None = None) -> None:
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
            "testGates": sanitize(copy.deepcopy((run.metadata or {}).get("testGates") or [])),
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
        metadata = run.metadata if isinstance(run.metadata, dict) else {}
        if "childSummary" in metadata:
            payload["childSummary"] = sanitize(copy.deepcopy(metadata["childSummary"]))
        if "executionOutcome" in metadata:
            payload["executionOutcome"] = metadata["executionOutcome"]
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
