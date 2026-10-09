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
from workflow_workspace import (
    WorkspacePathError,
    normalize_workspace_relative,
    resolve_workspace_child,
    resolve_workspace_root,
    workspace_metadata,
)
from workflow_check_adapters import run_check
from workflow_tool_profiles import CAPABILITY_CLASSES, tool_has_capability
from workflow_verification import validate_verification_contract


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



# Artifacts whose declared writer runs under a non-mutating profile. Step-Code
# solves this the same way: its QA/verifier agent is read-only and *returns*
# structured evidence, and the runtime persists it
# (workflow/runtime.ts writeEvidence -> journal.appendEvidence). GA used to
# require the verifier to write the file itself while the host forbade exactly
# that write, so a run where every agent succeeded still failed with
# missing_artifact.
NON_MUTATING_PROFILES = frozenset({"read_only", "verify"})


def _result_payload_for_write(job: WorkflowJob) -> Any:
    payload = (job.metadata or {}).get("result")
    if payload is None:
        transcript = (job.metadata or {}).get("handoff")
        payload = {"summary": str(transcript or "")}
    return payload


def write_contract_evidence_if_needed(run: WorkflowRun) -> list[str]:
    """Persist contract artifacts whose writer could not write, host-side.

    Returns the workspace-relative paths the host authored. Only artifacts that
    do not exist yet, whose declared writer is a completed job running a
    non-mutating permission profile, and whose payload is structured, are
    written. Everything else keeps the existing enforcement: a writer that
    *can* write still owns its own artifact, and a missing artifact still fails
    the run.
    """

    metadata = run.metadata if isinstance(run.metadata, dict) else {}
    contract = metadata.get("executionContract")
    if not isinstance(contract, dict) or contract.get("requiresExecution") is not True:
        return []
    workspace_raw = metadata.get("workspacePath")
    if not workspace_raw:
        return []
    try:
        workspace = resolve_workspace_root(workspace_raw)
    except (OSError, ValueError):
        return []
    jobs_by_label = {
        " ".join(str((job.metadata or {}).get("label") or "").split()): job
        for job in run.jobs
        if str((job.metadata or {}).get("label") or "").strip()
    }
    written: list[str] = []
    for artifact in contract.get("artifacts") or []:
        if not isinstance(artifact, dict):
            continue
        raw_path = str(artifact.get("path") or "").strip()
        writer_label = " ".join(str(artifact.get("writer") or "").split())
        if not raw_path or not writer_label:
            continue
        writer = jobs_by_label.get(writer_label)
        if writer is None or writer.status != "succeeded":
            continue
        profile = str((writer.metadata or {}).get("permissionProfile") or "").strip().lower()
        if profile not in NON_MUTATING_PROFILES:
            continue
        try:
            target = resolve_workspace_child(raw_path, workspace)
        except WorkspacePathError:
            continue
        if target.exists():
            continue
        payload = _result_payload_for_write(writer)
        if not isinstance(payload, (dict, list)):
            continue
        document = copy.deepcopy(payload)
        document = sanitize(document)
        document.setdefault("evidenceAuthor", writer_label)
        document["evidenceAuthorProfile"] = profile
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(json.dumps(document, ensure_ascii=False, indent=2), encoding="utf-8")
        except OSError:
            continue
        written.append(normalize_workspace_relative(raw_path, workspace))
    if written:
        metadata["hostAuthoredEvidence"] = written
        run.metadata = metadata
    return written

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
        workspace_root: str | Path | None = None,
    ):
        self.store = store or WorkflowStore()
        self.llm_binding_provider = llm_binding_provider
        self.workspace_root = resolve_workspace_root(workspace_root) if workspace_root is not None else resolve_workspace_root()
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
        if not run.artifact_dir:
            run = self.store.create_run(run)
        if workspace_path is None:
            saved_workspace = (run.metadata or {}).get("workspacePath") if isinstance(run.metadata, dict) else None
            if not saved_workspace and resume_from_run_id:
                try:
                    source_run = self.store.load_run(resume_from_run_id)
                    saved_workspace = (source_run.metadata or {}).get("workspacePath") if isinstance(source_run.metadata, dict) else None
                    if not saved_workspace and source_run.artifact_dir:
                        saved_workspace = str(Path(source_run.artifact_dir) / "workspace")
                except (FileNotFoundError, OSError, ValueError):
                    saved_workspace = None
            if saved_workspace:
                workspace = resolve_workspace_root(saved_workspace)
            else:
                workspace = self.workspace_root
            workspace_path = str(workspace)
        meta = run.metadata if isinstance(run.metadata, dict) else {}
        meta = dict(meta)
        meta.update(workspace_metadata(Path(workspace_path)))
        run.metadata = meta
        runtime_args = dict(args) if isinstance(args, dict) else ({} if args is None else args)
        if isinstance(runtime_args, dict):
            runtime_args["workspacePath"] = workspace_path
        if run.status in {"draft", "awaiting_approval"}:
            run.status = "running"
            self.store.save_run(run)
        else:
            self.store.save_run(run)
        resume_plan = self._build_resume_plan(run, args=runtime_args, cache_args=args, resume_from_run_id=resume_from_run_id)
        scheduler = AgentScheduler(
            store=self.store,
            run=run,
            runner=self.runner,
            config=self.scheduler_config,
            manage_run_completion=False,
            args=runtime_args,
            cache_args=args,
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
            self._send(process, {"type": "start", "script": run.script, "args": runtime_args, "timeoutMs": timeout_ms})
            while True:
                self._raise_if_deadline_expired(deadline)
                self._raise_if_externally_killed(run, scheduler, process)

                for completed_job in scheduler.tick(failure_policy="continue"):
                    self._complete_pending_rpc(process, scheduler, pending_rpc_jobs, completed_job)

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
                        args=runtime_args,
                        deadline=deadline,
                    )
                elif message_type == "event":
                    self._handle_worker_event(run, message)
                elif message_type == "done":
                    result = sanitize(message.get("result"))
                    self._last_worker_result = result
                    # Host-owned evidence persistence, before any contract check
                    # reads the filesystem. See write_contract_evidence_if_needed.
                    write_contract_evidence_if_needed(run)
                    gate_error = self._test_gate_failure_reason()
                    verification_error = self._explicit_verification_failure_reason(result)
                    execution_contract_error = self._evaluate_execution_contract_evidence(run)
                    artifact_integrity_error = self._evaluate_declared_artifact_integrity(run)
                    acceptance_error = self._evaluate_acceptance(run, result, args=runtime_args)
                    if gate_error or verification_error or execution_contract_error or artifact_integrity_error or acceptance_error:
                        metadata = dict(run.metadata) if isinstance(run.metadata, dict) else {}
                        metadata["integrationStatus"] = "rejected"
                        metadata["integrationIssues"] = [
                            item for item in (gate_error, verification_error, execution_contract_error, artifact_integrity_error, acceptance_error) if item
                        ]
                        metadata["finalAuditStatus"] = "failed"
                        run.metadata = metadata
                        raise RuntimeError(gate_error or verification_error or execution_contract_error or artifact_integrity_error or acceptance_error)
                    metadata = dict(run.metadata) if isinstance(run.metadata, dict) else {}
                    degraded = str(run.status or "") == "degraded" or bool(metadata.get("workflowIssues"))
                    metadata["integrationStatus"] = "degraded" if degraded else "accepted"
                    metadata["integrationIssues"] = []
                    # Non-full runs keep the historic "integration outcome"
                    # meaning; full runs overwrite this from the audit file. A
                    # degraded run never reports a clean pass.
                    metadata["finalAuditStatus"] = "degraded" if degraded else "passed"
                    run.metadata = metadata
                    run.status = metadata["integrationStatus"] if degraded else "succeeded"
                    run.error = None
                    refresh_workflow_execution_metadata(run)
                    self.store.save_run(run)
                    self.store.write_workflow_progress(run)
                    self._write_final_audit(run)
                    final_payload = self._final_payload(run, run.status, result=result)
                    self.store.write_final_result(run, final_payload)
                    self.store.save_run(run)
                    self._append(run, "workflow_finished", {
                        "status": run.status,
                        "outcome": (run.metadata or {}).get("executionOutcome", "succeeded"),
                        "integrationStatus": (run.metadata or {}).get("integrationStatus", "accepted"),
                        "auditStatus": (run.metadata or {}).get("finalAuditStatus", "passed"),
                        "resultRef": run.result_ref,
                        "artifactDir": run.artifact_dir,
                        "workspacePath": (run.metadata or {}).get("workspacePath"),
                        "blockingIssues": [],
                    })
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
                metadata = dict(run.metadata) if isinstance(run.metadata, dict) else {}
                metadata["integrationStatus"] = "cancelled"
                metadata["finalAuditStatus"] = "not_run"
                run.metadata = metadata
                refresh_workflow_execution_metadata(run)
                self.store.save_run(run)
                self.store.write_workflow_progress(run)
                self._write_final_audit(run)
                self.store.write_final_result(
                    run,
                    self._final_payload(run, "killed", result=self._last_worker_result, error=run.error),
                )
                self.store.save_run(run)
                self._append(run, "workflow_killed", {"error": run.error})
            else:
                run.status = "failed"
                run.error = reason
                metadata = dict(run.metadata) if isinstance(run.metadata, dict) else {}
                metadata["integrationStatus"] = "rejected"
                metadata["integrationIssues"] = [reason]
                metadata["finalAuditStatus"] = "failed"
                acceptance_contract = metadata.get("acceptanceContract")
                if isinstance(acceptance_contract, dict) and acceptance_contract.get("required"):
                    failures = list(metadata.get("acceptanceFailures") or [])
                    if reason not in failures:
                        failures.append(reason)
                    metadata["acceptanceStatus"] = "failed"
                    metadata["acceptanceFailures"] = failures
                run.metadata = metadata
                refresh_workflow_execution_metadata(run)
                self.store.save_run(run)
                self.store.write_workflow_progress(run)
                self._write_final_audit(run)
                self.store.write_final_result(
                    run,
                    self._final_payload(run, "failed", result=self._last_worker_result, error=reason),
                )
                self.store.save_run(run)
                self._append(run, "workflow_failed", {
                    "status": run.status,
                    "outcome": (run.metadata or {}).get("executionOutcome", "failed"),
                    "integrationStatus": (run.metadata or {}).get("integrationStatus", "rejected"),
                    "auditStatus": (run.metadata or {}).get("finalAuditStatus", "failed"),
                    "resultRef": run.result_ref,
                    "artifactDir": run.artifact_dir,
                    "blockingIssues": copy.deepcopy((run.metadata or {}).get("integrationIssues") or [reason]),
                    "error": reason,
                })
            raise
        finally:
            self._terminate(process)
            reader_done.set()
            if hasattr(self.runner, "clear_run_capabilities"):
                self.runner.clear_run_capabilities(run.run_id)

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
        required_tools = options.get("requiredTools") or []
        declared_capabilities = options.get("capabilities") or []
        if (required_tools or declared_capabilities) and hasattr(self.runner, "prepare_run_capabilities"):
            # Fail-soft, like Codex's MCP startup events and Step-Code's missing
            # tool construction: report coverage and let the model work with
            # what is connected. The run degrades visibly instead of dying, and
            # it never passes silently.
            snapshot = self.runner.prepare_run_capabilities(
                scheduler.run.run_id,
                required_tools,
                capabilities=declared_capabilities,
            )
            metadata = dict(scheduler.run.metadata) if isinstance(scheduler.run.metadata, dict) else {}
            unavailable = [str(item) for item in (snapshot.get("unavailableCapabilities") or [])]
            report = snapshot.get("capabilityReport") if isinstance(snapshot.get("capabilityReport"), dict) else {}
            missing_tools = [str(item) for item in (report.get("missingTools") or [])]
            if not metadata.get("capabilitySnapshot"):
                metadata["capabilitySnapshot"] = sanitize(snapshot)
            if unavailable:
                metadata["unavailableCapabilities"] = sorted(
                    {*(metadata.get("unavailableCapabilities") or []), *unavailable}
                )
            if missing_tools:
                metadata["missingRequiredTools"] = sorted(
                    {*(metadata.get("missingRequiredTools") or []), *missing_tools}
                )
            scheduler.run.metadata = metadata
            scheduler.store.save_run(scheduler.run)
            scheduler._append("workflow_capability_snapshot", payload=sanitize(snapshot))
            declared = {str(item) for item in declared_capabilities}
            degraded = sorted((set(unavailable) & declared) if declared else set())
            if degraded or missing_tools:
                scheduler.record_capability_degradation(
                    unavailable=degraded or unavailable,
                    missing_tools=missing_tools,
                )
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
                self._send(process, {"type": "rpc_result", "id": int(message.get("id")), "ok": True, "value": scheduler.downstream_result(job)})
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
        # A run that never declared test work cannot satisfy a python unittest
        # gate, and an empty repo is the normal shape for research/review plans.
        # Record that as not-applicable evidence instead of a failure; a plan
        # that DID declare tests and produced none stays a hard failure.
        # CPython's unittest exits 5 when discovery finds zero tests, so the
        # "empty repo" signal is testCount == 0 plus the NO TESTS RAN sentinel,
        # not a zero exit code.
        no_tests_discovered = result.get("testCount") == 0 and (
            result.get("returncode") in {0, 5}
            or "NO TESTS RAN" in f"{result.get('stdout') or ''}{result.get('stderr') or ''}"
        )
        if no_tests_discovered and not self._plan_declared_tests(run):
            result["notApplicable"] = True
            result["notApplicableReason"] = result.get("error") or "no tests discovered"
            result["error"] = None
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
            if result.get("gatePassed") or result.get("notApplicable"):
                continue
            return f"workflow test gate failed: {result.get('gateId')}: {self._test_gate_failure_preview(result)}"
        return None

    def _plan_declared_tests(self, run: WorkflowRun) -> bool:
        metadata = run.metadata if isinstance(run.metadata, dict) else {}
        contract = metadata.get("acceptanceContract")
        if not isinstance(contract, dict):
            return True
        return bool(contract.get("testsDeclared", True))

    @staticmethod
    def _explicit_verification_failure_reason(result: Any) -> str | None:
        if isinstance(result, dict) and result.get("verificationPassed") is False:
            return "workflow verification failed: verificationPassed=false"
        return None

    def _evaluate_acceptance(self, run: WorkflowRun, result: Any, *, args: Any = None) -> str | None:
        metadata = dict(run.metadata) if isinstance(run.metadata, dict) else {}
        verification_contract = metadata.get("verificationContract")
        if isinstance(verification_contract, dict):
            return self._evaluate_verification_contract(run, result, verification_contract, metadata, args=args)
        contract = metadata.get("acceptanceContract")
        if not isinstance(contract, dict) or not contract.get("required"):
            return None

        failures: list[str] = []
        checks = contract.get("checks") or []
        for check in checks:
            check_name = str(check.get("type") if isinstance(check, dict) else check or "").strip()
            if check_name == "python_unittest":
                if any(
                    gate.get("notApplicable") is True and gate.get("expectation") == "pass"
                    for gate in self._test_gates
                ):
                    metadata["notApplicableChecks"] = sorted(
                        {*(metadata.get("notApplicableChecks") or []), "python_unittest"}
                    )
                elif not any(
                    gate.get("gateKey") == "workflow-acceptance"
                    and gate.get("expectation") == "pass"
                    and gate.get("passed") is True
                    and gate.get("gatePassed") is True
                    for gate in self._test_gates
                ):
                    failures.append("required acceptance check python_unittest did not pass")
            elif check_name in {"verification", "verification_schema"}:
                verifications = self._find_verification_results(result)
                if not verifications:
                    failures.append("required acceptance check verification_schema is missing")
                elif any(item.get("verificationPassed") is not True for item in verifications):
                    failures.append("required acceptance check verification_schema did not pass")
            else:
                failures.append(f"unsupported required acceptance check: {check_name or '<empty>'}")

        if failures:
            metadata["acceptanceStatus"] = "failed"
            metadata["acceptanceFailures"] = failures
            run.metadata = metadata
            return "workflow acceptance failed: " + "; ".join(failures)
        not_applicable = metadata.get("notApplicableChecks") or []
        metadata["acceptanceStatus"] = "not_applicable" if not_applicable and len(not_applicable) == len(checks) else "passed"
        metadata["acceptanceFailures"] = []
        run.metadata = metadata
        return None

    def _evaluate_execution_contract_evidence(self, run) -> str | None:
        metadata = run.metadata if isinstance(run.metadata, dict) else {}
        contract = metadata.get("executionContract")
        if not isinstance(contract, dict) or contract.get("requiresExecution") is not True:
            return None
        transcript_cache: dict[str, list[dict]] = {}

        def events_for(label: str) -> list[dict]:
            wanted = " ".join(str(label or "").split()).casefold()
            job = next(
                (
                    item
                    for item in run.jobs
                    if " ".join(str((item.metadata or {}).get("label") or "").split()).casefold() == wanted
                ),
                None,
            )
            if not job:
                return []
            if label not in transcript_cache:
                transcript_cache[label] = self.store.read_agent_transcript_events(run, (job.metadata or {}).get("transcriptRef"))
            return transcript_cache[label]

        artifact_paths: set[str] = set()
        for artifact in contract.get("artifacts") or []:
            if not isinstance(artifact, dict) or not str(artifact.get("path") or "").strip():
                continue
            if "artifact_readback" not in {str(item) for item in artifact.get("requiredChecks") or []}:
                continue
            normalized_path = Path(str(artifact.get("path") or "").replace("\\", "/")).as_posix()
            artifact_paths.add(normalized_path)
            # Treat ./foo and foo as equivalent, but preserve a legitimate
            # leading dot in a filename such as .report.html.
            if normalized_path.startswith("./"):
                artifact_paths.add(normalized_path[2:])

        evidence_report: list[dict[str, object]] = []

        def code_run_readback_count(events: list[dict]) -> int:
            count = 0
            pending = False
            for event in events:
                if event.get("type") == "tool_call" and event.get("toolName") == "code_run":
                    script = str((event.get("args") or {}).get("script") or "").replace("\\", "/").lower()
                    # A successful code_run can read text, binary/ZIP/DOCX files,
                    # or inspect their existence/size. Treat those as semantic
                    # readback, not only literal file_read calls.
                    read_operation = any(token in script for token in (
                        "read_text", "read_bytes", ".open(", "open(", "document(",
                        "zipfile", "getsize(", "stat(", "exists(", "path.exists",
                    ))
                    pending = bool(script and read_operation and any(path.lower() in script for path in artifact_paths))
                elif pending and event.get("type") == "tool_result" and event.get("toolName") == "code_run":
                    data = event.get("data") or {}
                    if isinstance(data, dict) and data.get("status") == "success":
                        count += 1
                    pending = False
            return count

        missing_tools = {str(item) for item in (metadata.get("missingRequiredTools") or []) if str(item)}
        absent_capabilities = {str(item) for item in (metadata.get("unavailableCapabilities") or []) if str(item)}

        for evidence in contract.get("requiredToolEvidence") or []:
            if not isinstance(evidence, dict):
                continue
            tool = str(evidence.get("tool") or "")
            label = str(evidence.get("agent") or "")
            mode = str(evidence.get("mode") or "").strip().lower()
            if not mode:
                mode = "preferred" if tool in {"file_write", "file_patch", "file_read", "code_run"} else "required"
            minimum_calls = max(1, int(evidence.get("minimumCalls") or 1))
            events = events_for(label)
            calls = sum(1 for event in events if event.get("type") == "tool_call" and event.get("toolName") == tool)
            if tool == "file_read" and calls < minimum_calls:
                calls += code_run_readback_count(events)
            observed = calls >= minimum_calls
            evidence_report.append({"tool": tool, "agent": label, "mode": mode, "requiredCalls": minimum_calls, "observedCalls": calls, "satisfied": observed})
            if observed or mode != "required":
                continue
            if tool in missing_tools:
                # The environment never had this tool. The preflight already
                # recorded it as a degradation; blaming the child would be wrong.
                evidence_report[-1]["reason"] = "capability_unavailable"
                continue
            return f"missing_required_tool_evidence: {tool} expected at least {minimum_calls} call(s) from {label}"

        for evidence in contract.get("requiredCapabilityEvidence") or []:
            if not isinstance(evidence, dict):
                continue
            capability = str(evidence.get("capability") or "").strip()
            label = str(evidence.get("agent") or "")
            mode = str(evidence.get("mode") or "").strip().lower() or "required"
            minimum_calls = max(1, int(evidence.get("minimumCalls") or 1))
            if capability not in CAPABILITY_CLASSES:
                return f"invalid_required_capability_evidence: unknown capability {capability or '<missing>'}"
            events = events_for(label)
            calls = sum(
                1
                for event in events
                if event.get("type") == "tool_call" and tool_has_capability(event.get("toolName"), capability)
            )
            if capability == "file_read" and calls < minimum_calls:
                calls += code_run_readback_count(events)
            observed = calls >= minimum_calls
            entry = {
                "capability": capability,
                "agent": label,
                "mode": mode,
                "requiredCalls": minimum_calls,
                "observedCalls": calls,
                "satisfied": observed,
            }
            evidence_report.append(entry)
            if observed or mode != "required":
                continue
            if capability in absent_capabilities:
                entry["reason"] = "capability_unavailable"
                continue
            return f"missing_required_capability_evidence: {capability} expected at least {minimum_calls} call(s) from {label}"

        metadata["executionToolEvidence"] = evidence_report
        run.metadata = metadata

        workspace_raw = metadata.get("workspacePath")
        if contract.get("artifacts") and not workspace_raw:
            return "missing_artifact_workspace: execution contract requires a workspacePath"
        workspace = resolve_workspace_root(workspace_raw) if workspace_raw else None
        if workspace:
            for artifact in contract.get("artifacts") or []:
                if not isinstance(artifact, dict):
                    continue
                raw_path = str(artifact.get("path") or "").strip()
                if not raw_path:
                    continue
                try:
                    relative = normalize_workspace_relative(raw_path, workspace)
                    target = resolve_workspace_child(raw_path, workspace)
                except WorkspacePathError:
                    # Keep the contract error classification stable while
                    # routing all path semantics through the shared resolver.
                    artifact_paths.add(raw_path.replace("\\", "/"))
                    continue
                artifact_paths.add(relative)
                artifact_paths.add(str(target).replace("\\", "/"))
        explicit_file_readers = {
            str(evidence.get("agent") or "")
            for evidence in contract.get("requiredToolEvidence") or []
            if isinstance(evidence, dict)
            and str(evidence.get("tool") or "") == "file_read"
            and str(evidence.get("agent") or "")
        }
        for artifact in contract.get("artifacts") or []:
            raw_path = str(artifact.get("path") or "").strip()
            relative_path = raw_path.replace("\\", "/")
            if not relative_path:
                return "invalid_artifact_path: <missing>"
            try:
                relative_path = normalize_workspace_relative(raw_path, workspace)
                target = resolve_workspace_child(raw_path, workspace)
            except WorkspacePathError:
                return f"invalid_artifact_path: {relative_path}"
            checks = {str(item) for item in artifact.get("requiredChecks") or []}
            # Every artifact declared in the execution contract is a delivered
            # product, so existence is checked by the host unconditionally.
            # The old behavior only checked when the planner happened to write
            # artifact_exists into requiredChecks, which let a run report
            # success while its declared artifact was never produced.
            declares_optional = bool(artifact.get("optional")) or "artifact_optional" in checks
            if not declares_optional:
                if "artifact_exists" not in checks:
                    checks.add("artifact_exists")
                if not target.is_file():
                    return f"missing_artifact: {relative_path}"
                # A declared deliverable that exists but is empty is not a
                # delivery. The 2026-10-09 failure destroyed the report body and
                # left a placeholder behind; existence alone did not notice.
                if target.stat().st_size == 0:
                    return f"empty_artifact: {relative_path}"
            if "artifact_readback" in checks:
                filename = target.name.lower()
                # Readback may be performed by a synthesis/review child whose
                # role label is model-defined. Search every completed job; the
                # artifact path match below prevents an unrelated file read from
                # satisfying this check.
                reader_labels = {str(artifact.get("writer") or "")} | explicit_file_readers
                reader_labels.update(
                    str((job.metadata or {}).get("label") or "")
                    for job in run.jobs
                    if str((job.metadata or {}).get("label") or "")
                )
                reader_events = [event for label in reader_labels for event in events_for(label)]
                read_calls = [
                    event for event in reader_events
                    if event.get("type") == "tool_call" and event.get("toolName") == "file_read"
                ]
                direct_read = any(filename in json.dumps(event.get("args") or {}, ensure_ascii=False).lower() for event in read_calls)
                all_job_events = []
                for job in run.jobs:
                    label = str((job.metadata or {}).get("label") or "")
                    if label:
                        all_job_events.extend(events_for(label))
                code_read = code_run_readback_count(all_job_events or reader_events) > 0
                if not direct_read and not code_read:
                    return f"missing_artifact_readback_evidence: {relative_path}"
        return None

    def _declared_artifact_paths(self, run) -> list[str]:
        metadata = run.metadata if isinstance(run.metadata, dict) else {}
        contract = metadata.get("executionContract")
        if not isinstance(contract, dict):
            return []
        paths: list[str] = []
        for artifact in contract.get("artifacts") or []:
            if not isinstance(artifact, dict) or artifact.get("optional"):
                continue
            raw = str(artifact.get("path") or "").strip().replace("\\", "/")
            if raw and raw not in paths:
                paths.append(raw)
        return paths

    def _evaluate_declared_artifact_integrity(self, run) -> str | None:
        """Reject a declared artifact that was overwritten after a successful write.

        Reproduces the 2026-10-09 failure deterministically: the writer produced
        the report, a later job rewrote the same path with a placeholder, and
        the deliverable was destroyed while existence and readback stayed green.
        Here the host pairs each ``file_write``/``file_patch`` tool call with its
        result and records the largest size it ever observed for a declared
        artifact; if the final on-disk file is smaller than that, the artifact
        was altered after delivery and the contract fails.
        """

        declared = self._declared_artifact_paths(run)
        if not declared:
            return None
        metadata = run.metadata if isinstance(run.metadata, dict) else {}
        workspace_raw = metadata.get("workspacePath")
        if not workspace_raw:
            return None
        try:
            workspace = resolve_workspace_root(workspace_raw)
        except WorkspacePathError:
            return None

        observed_max: dict[str, int] = {}
        for job in run.jobs:
            events = self.store.read_agent_transcript_events(run, (job.metadata or {}).get("transcriptRef"))
            pending_path: str | None = None
            for event in events:
                tool_name = str(event.get("toolName") or "")
                if event.get("type") == "tool_call" and tool_name in {"file_write", "file_patch"}:
                    pending_path = self._declared_match(str((event.get("args") or {}).get("path") or ""), declared)
                    continue
                if event.get("type") != "tool_result" or tool_name not in {"file_write", "file_patch"}:
                    continue
                data = event.get("data")
                written = data.get("writed_bytes") if isinstance(data, dict) else None
                if (
                    pending_path
                    and isinstance(data, dict)
                    and str(data.get("status") or "") == "success"
                    and isinstance(written, int)
                ):
                    observed_max[pending_path] = max(observed_max.get(pending_path, 0), written)
                pending_path = None

        for relative_path in declared:
            try:
                target = resolve_workspace_child(relative_path, workspace)
            except WorkspacePathError:
                return f"invalid_artifact_path: {relative_path}"
            if not target.is_file():
                return f"missing_artifact: {relative_path}"
            observed = observed_max.get(relative_path)
            if observed and observed > 0:
                actual = target.stat().st_size
                if actual < observed:
                    return f"artifact_altered_after_write: {relative_path} (was {observed} bytes, now {actual})"
        return None

    @staticmethod
    def _declared_match(raw_path: str, declared: list[str]) -> str | None:
        normalized = str(raw_path or "").strip().replace("\\", "/")
        if normalized.startswith("./"):
            normalized = normalized[2:]
        for declared_path in declared:
            if normalized == declared_path or normalized.endswith("/" + declared_path):
                return declared_path
        return None

    def _evaluate_verification_contract(self, run, result, raw_contract, metadata, *, args=None):
        try:
            contract = validate_verification_contract(raw_contract)
        except ValueError as exc:
            metadata["acceptanceStatus"] = "failed"
            metadata["acceptanceFailures"] = [f"invalid verification contract: {exc}"]
            run.metadata = metadata
            return metadata["acceptanceFailures"][0]

        supplied = {}
        if isinstance(metadata.get("verificationEvidence"), dict):
            supplied.update(metadata["verificationEvidence"])
        if isinstance(result, dict) and isinstance(result.get("verificationEvidence"), dict):
            supplied.update(result["verificationEvidence"])
        failures = []
        evidence = dict(supplied)
        for check in contract["checks"]:
            if not check.get("required"):
                continue
            check_id = check["id"]
            item = evidence.get(check_id)
            if item is None and check.get("kind") == "schema":
                verifications = self._find_verification_results(result)
                if verifications:
                    item = {"status": "passed" if all(value.get("verificationPassed") is True for value in verifications) else "failed", "evidence": verifications}
            if item is None and check.get("kind") == "command" and check.get("adapter") == "python_unittest":
                item = next(
                    ({"status": "passed" if gate.get("gatePassed") or gate.get("notApplicable") else "failed", "evidence": gate} for gate in reversed(self._test_gates)),
                    None,
                )
            if item is None and check.get("kind") in {"command", "artifact"}:
                workspace = normalize_workflow_workspace(args)
                if workspace:
                    item = run_check(check, workspace=workspace, timeout_s=self.timeout_seconds)
            if item is None:
                failures.append(f"required verification check {check_id} has no evidence")
                continue
            evidence[check_id] = item
            if isinstance(item, dict):
                status = item.get("status")
                passed = status == "passed" or item.get("passed") is True or item.get("gatePassed") is True
            else:
                passed = item is True
            if not passed:
                failures.append(f"required verification check {check_id} did not pass")
        metadata["verificationContract"] = contract
        metadata["verificationEvidence"] = evidence
        if failures:
            metadata["acceptanceStatus"] = "failed"
            metadata["acceptanceFailures"] = failures
            run.metadata = metadata
            return "workflow verification failed: " + "; ".join(failures)
        metadata["acceptanceStatus"] = "passed" if contract["checks"] else "not_applicable"
        metadata["acceptanceFailures"] = []
        run.metadata = metadata
        return None

    @classmethod
    def _find_verification_results(cls, value: Any) -> list[dict]:
        found: list[dict] = []
        if isinstance(value, dict):
            if "verificationPassed" in value:
                found.append(value)
            else:
                for child in value.values():
                    found.extend(cls._find_verification_results(child))
        elif isinstance(value, list):
            for child in value:
                found.extend(cls._find_verification_results(child))
        return found

    @staticmethod
    def _test_gate_failure_preview(result: dict) -> str:
        text = result.get("error") or result.get("stderr") or result.get("stdout") or "test gate did not pass"
        text = redact_sensitive_text(str(text)).strip()
        return text[:2_000]

    def _complete_pending_rpc(self, process: subprocess.Popen, scheduler: AgentScheduler, pending_rpc_jobs: dict[int, WorkflowJob], job: WorkflowJob) -> None:
        rpc_id = None
        for candidate_id, candidate_job in pending_rpc_jobs.items():
            if candidate_job.job_id == job.job_id:
                rpc_id = candidate_id
                break
        if rpc_id is None:
            return
        pending_rpc_jobs.pop(rpc_id, None)
        if job.status in {"succeeded", "degraded"}:
            self._send(process, {"type": "rpc_result", "id": rpc_id, "ok": True, "value": scheduler.downstream_result(job)})
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

    def _build_resume_plan(self, run: WorkflowRun, *, args: Any = None, cache_args: Any = None, resume_from_run_id: str | None = None) -> list[dict]:
        if not resume_from_run_id or resume_from_run_id == run.run_id:
            return []
        try:
            source_run = self.store.load_run(resume_from_run_id)
        except Exception:
            return []
        if source_run.session_id != run.session_id:
            return []
        plan: list[dict] = []
        probe_scheduler = AgentScheduler(store=self.store, run=run, runner=self.runner, config=self.scheduler_config, manage_run_completion=False, args=args, cache_args=cache_args)
        for source_job in source_run.jobs:
            # Only clean deliveries form a reusable prefix. A degraded job is a
            # partial-fidelity result; replaying it as a "cached success" would
            # silently propagate a lower-quality prefix into the resumed run.
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
                "workspacePathHash",
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

    def _write_final_audit(self, run: WorkflowRun) -> str | None:
        """Emit final-audit.md only for full-contract runs (host-recorded facts)."""

        metadata = run.metadata if isinstance(run.metadata, dict) else {}
        eval_contract = metadata.get("evalContract") if isinstance(metadata.get("evalContract"), dict) else {}
        if str(eval_contract.get("level") or "").strip().lower() != "full":
            return None
        payload = {"status": run.status, "testGates": copy.deepcopy(metadata.get("testGates") or [])}
        return self.store.write_final_audit(run, payload)

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
        if "acceptanceStatus" in metadata:
            payload["acceptanceStatus"] = metadata["acceptanceStatus"]
            payload["acceptanceFailures"] = sanitize(copy.deepcopy(metadata.get("acceptanceFailures") or []))
        if "verificationContract" in metadata:
            payload["verificationContract"] = sanitize(copy.deepcopy(metadata["verificationContract"]))
        if "verificationEvidence" in metadata:
            payload["verificationEvidence"] = sanitize(copy.deepcopy(metadata["verificationEvidence"]))
        for key in ("integrationStatus", "integrationIssues", "finalAuditStatus", "finalAuditRef"):
            if key in metadata:
                payload[key] = sanitize(copy.deepcopy(metadata[key]))
        eval_contract = metadata.get("evalContract") if isinstance(metadata.get("evalContract"), dict) else {}
        if eval_contract:
            payload["evalContract"] = sanitize(copy.deepcopy(eval_contract))
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
