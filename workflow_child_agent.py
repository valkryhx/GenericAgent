from __future__ import annotations

import copy

from workflow_models import AgentResult


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
