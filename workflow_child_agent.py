from __future__ import annotations

import copy
import threading
import time
from typing import Protocol

from workflow_models import AgentResult


class ChildAgentRunner(Protocol):
    def start(self, job) -> None: ...
    def poll(self, job) -> AgentResult | None: ...
    def cancel(self, job) -> None: ...


class FakeChildAgentRunner:
    def __init__(
        self,
        *,
        delay_ticks: int = 0,
        results: dict[str, dict] | None = None,
        fail_job_ids: set[str] | None = None,
        cancellable: bool = True,
    ):
        self.delay_ticks = max(0, int(delay_ticks))
        self.results = copy.deepcopy(results or {})
        self.fail_job_ids = set(fail_job_ids or set())
        self.cancellable = bool(cancellable)
        self._remaining: dict[str, int] = {}
        self.cancelled_job_ids: set[str] = set()

    def start(self, job) -> None:
        self._remaining[job.job_id] = self.delay_ticks

    def cancel(self, job) -> None:
        if self.cancellable:
            self.cancelled_job_ids.add(job.job_id)
            self._remaining[job.job_id] = 0

    def poll(self, job) -> AgentResult | None:
        if job.job_id in self.cancelled_job_ids:
            return AgentResult(job_id=job.job_id, status="cancelled", payload={"cancelled": True})
        remaining = self._remaining.get(job.job_id, 0)
        if remaining > 0:
            self._remaining[job.job_id] = remaining - 1
            return None
        if job.job_id in self.fail_job_ids:
            raise RuntimeError(f"fake child agent failed: {job.job_id}")
        payload = copy.deepcopy(self.results.get(job.job_id, {"summary": f"completed {job.job_id}"}))
        return AgentResult(job_id=job.job_id, payload=payload)


class NativeGPTChildAgentRunner:
    def __init__(
        self,
        *,
        config_name: str = "native_oai_config",
        session_factory=None,
        system_prompt: str | None = None,
        max_tokens: int | None = None,
    ):
        self.config_name = config_name
        self.session_factory = session_factory
        self.system_prompt = system_prompt
        self.max_tokens = max_tokens
        self._states: dict[str, dict] = {}
        self._lock = threading.Lock()

    def start(self, job) -> None:
        session = self._new_session()
        if self.max_tokens is not None and hasattr(session, "max_tokens"):
            session.max_tokens = self.max_tokens
        if self.system_prompt is not None and hasattr(session, "system"):
            session.system = self.system_prompt
        state = {"session": session, "result": None, "done": False}
        with self._lock:
            self._states[job.job_id] = state
        thread = threading.Thread(target=self._run_job, args=(job, state), daemon=True)
        state["thread"] = thread
        thread.start()

    def poll(self, job) -> AgentResult | None:
        with self._lock:
            state = self._states.get(job.job_id)
            if not state or not state.get("done"):
                return None
            return state.get("result")

    def cancel(self, job) -> None:
        with self._lock:
            state = self._states.get(job.job_id)
        if not state:
            return
        session = state.get("session")
        if hasattr(session, "cancel_current_request"):
            session.cancel_current_request()

    def _new_session(self):
        if self.session_factory is not None:
            try:
                return self.session_factory(self.config_name)
            except TypeError:
                return self.session_factory()
        from llmcore import resolve_session
        return resolve_session(self.config_name)

    def _run_job(self, job, state: dict) -> None:
        session = state["session"]
        transcript_events: list[dict] = []
        prompt = self._build_prompt(job)
        transcript_ref = f"agents/{job.job_id}/transcript.jsonl"
        started_at = time.time()
        transcript_events.append(
            {
                "type": "metadata",
                "runId": job.metadata.get("runId"),
                "jobId": job.job_id,
                "phase": job.phase,
                "label": job.metadata.get("label"),
                "options": copy.deepcopy(job.metadata.get("options") or {}),
                "configName": self.config_name,
                "startedAt": started_at,
            }
        )
        message = {"role": "user", "content": [{"type": "text", "text": prompt}]}
        transcript_events.append({"type": "request", "messages": [copy.deepcopy(message)]})
        try:
            answer = "".join(str(chunk) for chunk in session.ask(message))
            usage = copy.deepcopy(getattr(session, "last_usage_tokens", None) or {})
            transcript_events.append({"type": "assistant", "text": answer})
            if usage:
                transcript_events.append({"type": "token_usage", "tokenUsage": usage})
            payload = {"summary": answer.strip(), "text": answer}
            result = AgentResult(
                job_id=job.job_id,
                status="succeeded",
                payload=payload,
                transcript_ref=transcript_ref,
                token_usage=usage,
                tool_summary={},
                transcript_events=transcript_events,
            )
        except Exception as exc:
            transcript_events.append({"type": "error", "error": str(exc)})
            result = AgentResult(
                job_id=job.job_id,
                status="failed",
                payload={"error": str(exc)},
                transcript_ref=transcript_ref,
                token_usage=copy.deepcopy(getattr(session, "last_usage_tokens", None) or {}),
                tool_summary={},
                transcript_events=transcript_events,
            )
        with self._lock:
            state["result"] = result
            state["done"] = True

    def _build_prompt(self, job) -> str:
        run_id = job.metadata.get("runId") or job.metadata.get("run_id") or ""
        label = job.metadata.get("label") or ""
        options = copy.deepcopy(job.metadata.get("options") or {})
        lines = [
            "You are a workflow child agent. Complete only this assigned job and return a concise result.",
            f"runId: {run_id}",
            f"jobId: {job.job_id}",
            f"phase: {job.phase or ''}",
            f"label: {label}",
            f"options: {options}",
            "",
            "Task:",
            job.prompt,
        ]
        return "\n".join(lines)
