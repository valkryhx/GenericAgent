"""Real-LLM E2E for per-agent maxTurns and max-turn retry escalation.

Opt in with ``GA_RUN_REAL_WORKFLOW_MAX_TURNS_E2E=1``. The child uses the local
``llm.yaml`` default profile, so this exercises the same binding production
workflows use rather than a stub client.

The first job deliberately starts with one step and must call ``file_read``.
That makes the first attempt exhaust its budget deterministically. Its retry
must start with 41 steps and succeed. A second job omits ``maxTurns`` and proves
the default 80 budget reaches the child. Only sanitized statuses, budget values,
tool names, and token usage are printed.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from genericagent.core.redaction import sanitize  # noqa: E402
from genericagent.workflow.child_agent import NativeGPTChildAgentRunner  # noqa: E402
from genericagent.workflow.llm import binding_from_profile  # noqa: E402
from genericagent.workflow.models import WorkflowRun  # noqa: E402
from genericagent.workflow.runtime import WorkflowRuntime  # noqa: E402
from genericagent.workflow.scheduler import SchedulerConfig  # noqa: E402
from genericagent.workflow.store import WorkflowStore  # noqa: E402

PROFILE = os.environ.get("GA_WORKFLOW_LLM_PROFILE") or "default"
OPT_IN = os.environ.get("GA_RUN_REAL_WORKFLOW_MAX_TURNS_E2E") == "1"
MARKER = "MAX_TURNS_E2E_MARKER_7F3A"

WORKFLOW_SCRIPT = r"""
export const meta = {
  name: 'max-turns-retry-e2e',
  description: 'verify per-agent maxTurns and max-turn retry escalation'
}

const forcedRetry = await agent(`This is a budget-contract test, not a research task.
First call file_read exactly once on the workspace file max_turns_marker.txt.
After reading it, report the marker value in your final answer.
Do not call any other tool. Do not write files.`, {
  label: 'forced-max-turn-retry',
  maxTurns: 1,
  retryPolicy: {
    maxAttempts: 2,
    retryableErrors: ['max_turns_exceeded'],
    backoffMs: 0
  }
})

const defaultBudget = await agent(`This is a budget-contract test. Do not call tools.
Recite the exact marker string provided here: ${args.marker}
Return only that marker as your final answer.`, {
  label: 'default-budget',
  dependsOn: ['forced-max-turn-retry']
})

return {
  forcedRetrySummary: forcedRetry.summary,
  defaultBudgetSummary: defaultBudget.summary
}
"""


def _journal_events(run: WorkflowRun) -> list[dict[str, Any]]:
    path = Path(run.artifact_dir) / "journal.jsonl"
    if not path.is_file():
        return []
    events: list[dict[str, Any]] = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(item, dict):
            events.append(item)
    return events


def _transcript_tools(store: WorkflowStore, run: WorkflowRun, job) -> list[str]:
    ref = (job.metadata or {}).get("transcriptRef")
    events = store.read_agent_transcript_events(run, ref)
    return sorted(
        {
            str(event.get("toolName") or event.get("tool_name") or "")
            for event in events
            if event.get("type") == "tool_call"
        }
        - {""}
    )


def _attempt_budgets(events: list[dict[str, Any]], job_id: str) -> list[int]:
    budgets: list[int] = []
    for item in events:
        if item.get("jobId") != job_id:
            continue
        event_type = str(item.get("type") or item.get("eventType") or "")
        payload = item.get("payload") if isinstance(item.get("payload"), dict) else {}
        if event_type == "agent_retry_scheduled" and isinstance(payload.get("maxTurns"), int):
            budgets.append(int(payload["maxTurns"]))
    return budgets


def main() -> int:
    summary: dict[str, Any] = {
        "passed": False,
        "skipped": not OPT_IN,
        "profileRequested": PROFILE,
        "issues": [],
    }
    if not OPT_IN:
        summary["reason"] = "set GA_RUN_REAL_WORKFLOW_MAX_TURNS_E2E=1 to run the real default-profile E2E"
        print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
        return 0

    binding = binding_from_profile(PROFILE)
    summary["binding"] = binding.as_metadata()
    root = Path(tempfile.mkdtemp(prefix="ga_real_max_turns_e2e_"))
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "max_turns_marker.txt").write_text(MARKER + "\n", encoding="utf-8")
    store = WorkflowStore(root=root / "runtime")
    run = store.create_run(
        WorkflowRun(
            run_id="wf_real_max_turns_retry",
            session_id="real_max_turns_retry",
            script=WORKFLOW_SCRIPT,
            status="running",
        )
    )
    runner = NativeGPTChildAgentRunner(profile_name=PROFILE, max_tokens=512)
    budgets_by_job: dict[str, int] = {}
    original_job_max_turns = runner._job_max_turns

    def record_job_max_turns(job):
        value = original_job_max_turns(job)
        budgets_by_job[job.job_id] = value
        return value

    runner._job_max_turns = record_job_max_turns
    try:
        outcome = WorkflowRuntime(
            store=store,
            runner=runner,
            scheduler_config=SchedulerConfig(max_concurrent=1, max_total=4),
            timeout_seconds=360.0,
        ).run(run, args={"workspacePath": str(workspace), "marker": MARKER})
        loaded = store.load_run(run.run_id)
        events = _journal_events(loaded)
        jobs = [
            {
                "jobId": job.job_id,
                "label": job.metadata.get("label"),
                "status": job.status,
                "maxTurns": (job.metadata.get("options") or {}).get("maxTurns"),
                "retryPolicy": sanitize(job.metadata.get("retryPolicy") or {}),
                "tools": _transcript_tools(store, loaded, job),
                "tokenUsage": sanitize(job.metadata.get("tokenUsage") or {}),
            }
            for job in loaded.jobs
        ]
        retry_events = [
            {
                "jobId": item.get("jobId"),
                "payload": sanitize(item.get("payload") or {}),
            }
            for item in events
            if str(item.get("type") or item.get("eventType") or "") == "agent_retry_scheduled"
        ]
        summary.update(
            {
                "runStatus": loaded.status,
                "result": sanitize(outcome.result),
                "jobs": jobs,
                "retryEvents": retry_events,
                "observedBudgets": dict(budgets_by_job),
                "workspacePath": str(workspace),
                "artifactDir": str(loaded.artifact_dir),
            }
        )
        forced = next((job for job in loaded.jobs if (job.metadata or {}).get("label") == "forced-max-turn-retry"), None)
        default_job = next((job for job in loaded.jobs if (job.metadata or {}).get("label") == "default-budget"), None)
        if loaded.status != "succeeded":
            summary["issues"].append("workflow_not_succeeded")
        if forced is None or forced.status != "succeeded":
            summary["issues"].append("forced_max_turn_job_not_succeeded")
        else:
            if forced.metadata.get("retryPolicy", {}).get("attempts") != 2:
                summary["issues"].append("forced_job_attempt_count_not_two")
            if _attempt_budgets(events, forced.job_id) != [41]:
                summary["issues"].append("retry_budget_not_41")
            if "file_read" not in _transcript_tools(store, loaded, forced):
                summary["issues"].append("forced_first_attempt_did_not_read_marker")
        if default_job is None or default_job.status != "succeeded":
            summary["issues"].append("default_budget_job_not_succeeded")
        if default_job is not None and (default_job.metadata.get("options") or {}).get("maxTurns") is not None:
            summary["issues"].append("default_job_had_explicit_max_turns")
        if default_job is not None and budgets_by_job.get(default_job.job_id) != 80:
            summary["issues"].append("default_runner_budget_not_80")
        if forced is not None and budgets_by_job.get(forced.job_id) != 41:
            summary["issues"].append("retried_runner_budget_not_41")
        summary["passed"] = not summary["issues"]
    except Exception as exc:
        summary["error"] = f"{type(exc).__name__}: {str(exc)[:500]}"
        try:
            loaded = store.load_run(run.run_id)
            summary["runStatus"] = loaded.status
            summary["jobStatuses"] = [job.status for job in loaded.jobs]
            summary["artifactDir"] = str(loaded.artifact_dir)
        except Exception:
            pass
    print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
    return 0 if summary["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())