from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from workflow_child_agent import FakeChildAgentRunner
from workflow_models import WorkflowEvent, WorkflowRun
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
    ):
        self.store = store or WorkflowStore()
        self.runner = runner or FakeChildAgentRunner()
        self.scheduler_config = scheduler_config or SchedulerConfig()
        self.worker_path = Path(worker_path) if worker_path else Path(__file__).resolve().with_name("workflow_js_worker.js")
        self.timeout_seconds = float(timeout_seconds)
        self._logs: list[str] = []
        self._phases: list[str] = []

    def run(self, run: WorkflowRun, *, args: Any = None) -> WorkflowRuntimeResult:
        self._scan_script(run.script)
        if not run.artifact_dir:
            run = self.store.create_run(run)
        if run.status in {"draft", "awaiting_approval"}:
            run.status = "running"
            self.store.save_run(run)
        scheduler = AgentScheduler(store=self.store, run=run, runner=self.runner, config=self.scheduler_config)
        process = subprocess.Popen(
            [self._node_executable(), str(self.worker_path)],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        try:
            ready = self._read_message(process)
            if ready.get("type") != "ready":
                raise RuntimeError(f"workflow worker did not become ready: {ready}")
            self._send(process, {"type": "start", "script": run.script, "args": args})
            while True:
                message = self._read_message(process)
                message_type = message.get("type")
                if message_type == "rpc":
                    value = self._handle_rpc(scheduler, message)
                    self._send(process, {"type": "rpc_result", "id": message.get("id"), "ok": True, "value": value})
                elif message_type == "event":
                    self._handle_worker_event(run, message)
                elif message_type == "done":
                    final_payload = {"runId": run.run_id, "status": "succeeded", "result": message.get("result")}
                    self.store.write_final_result(run, final_payload)
                    run.status = "succeeded"
                    self.store.save_run(run)
                    return WorkflowRuntimeResult(run=run, result=message.get("result"), logs=list(self._logs), phases=list(self._phases))
                elif message_type == "error":
                    raise RuntimeError(message.get("error") or "workflow worker failed")
        except Exception as exc:
            run.status = "failed"
            run.error = str(exc)
            self.store.write_final_result(run, {"runId": run.run_id, "status": "failed", "error": str(exc)})
            self.store.save_run(run)
            self._append(run, "workflow_failed", {"error": str(exc)})
            raise
        finally:
            self._terminate(process)

    def _handle_rpc(self, scheduler: AgentScheduler, message: dict) -> Any:
        method = message.get("method")
        params = message.get("params") or {}
        if method != "agent":
            raise RuntimeError(f"unsupported workflow rpc: {method}")
        job = scheduler.register_agent(prompt=str(params.get("prompt") or ""), options=params.get("options") or {})
        scheduler.run_all()
        if job.status != "succeeded":
            raise RuntimeError(job.error or f"workflow agent failed: {job.job_id}")
        return job.metadata.get("result") or {}

    def _handle_worker_event(self, run: WorkflowRun, message: dict) -> None:
        method = message.get("method")
        params = message.get("params") or {}
        if method == "phase":
            name = str(params.get("name") or "")
            self._phases.append(name)
            self._append(run, "workflow_phase", {"name": name})
        elif method == "log":
            text = str(params.get("message") or "")
            self._logs.append(text)
            self._append(run, "workflow_log", {"message": text})

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

    def _terminate(self, process: subprocess.Popen) -> None:
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                process.kill()
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is not None and not stream.closed:
                stream.close()

    @staticmethod
    def _node_executable() -> str:
        return "node.exe" if sys.platform.startswith("win") else "node"
