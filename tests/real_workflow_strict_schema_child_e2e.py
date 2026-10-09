"""Real-LLM E2E for the strict-schema child contract.

Opt in with ``GA_RUN_REAL_WORKFLOW_STRICT_SCHEMA_E2E=1``. Defaults to the local
``llm.yaml`` ``profiles.default`` and refuses to run when the resolved model does
not match ``GA_REAL_API_EXPECTED_MODEL``.

Regression target (2026-10-09): a research workflow whose plan declared
``SOURCE_SCHEMA`` (required: sources/claims/risks) failed the whole run because
the child was never told its answer is machine-validated JSON. It answered in
prose, and the display compactor then shrank its fenced JSON block to
``... (N lines)`` before validation. Both paths are exercised here with a real
child: a strict schema job must end ``succeeded`` with a JSON payload.

Only sanitized statuses, artifact names and tool names are printed.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from sensitive_redaction import sanitize  # noqa: E402
from workflow_llm import binding_from_profile  # noqa: E402
from workflow_models import WorkflowRun  # noqa: E402
from workflow_child_agent import NativeGPTChildAgentRunner  # noqa: E402
from workflow_runtime import WorkflowRuntime  # noqa: E402
from workflow_scheduler import SchedulerConfig  # noqa: E402
from workflow_store import WorkflowStore  # noqa: E402

PROFILE = os.environ.get("GA_WORKFLOW_LLM_PROFILE") or "default"
OPT_IN = os.environ.get("GA_RUN_REAL_WORKFLOW_STRICT_SCHEMA_E2E") == "1"


def check_profile(summary: dict[str, Any]) -> bool:
    binding = binding_from_profile(PROFILE)
    expected = os.environ.get("GA_REAL_API_EXPECTED_MODEL", "")
    summary["profile"] = {
        "profileName": binding.profile_name,
        "model": binding.model_id,
        "expectedModel": expected or None,
    }
    summary["profileOk"] = bool(binding.model_id) and (not expected or binding.model_id == expected)
    return summary["profileOk"]


STRICT_SCHEMA_SCRIPT = r"""
phase('Source Discovery')
const result = await agent(`This is a research assignment. Do not call tools; answer from general knowledge only.
Task: list three fictional-but-well-formed bibliographic sources, two claims, and one risk.
Return sources, claims and risks as structured data.
Do not read mykey.py, mykey.json, mcp.json, API keys, tokens, or credentials. Do not write files.`, {
  label: 'source-discovery',
  schema: {
    type: 'object',
    required: ['sources', 'claims', 'risks'],
    properties: {
      sources: { type: 'array', items: { type: 'object' } },
      claims: { type: 'array', items: { type: 'object' } },
      risks: { type: 'array' }
    }
  }
})
return {
  sourceCount: Array.isArray(result.sources) ? result.sources.length : -1,
  claimCount: Array.isArray(result.claims) ? result.claims.length : -1,
  riskCount: Array.isArray(result.risks) ? result.risks.length : -1
}
"""


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="ga_workflow_strict_schema_e2e_"))
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {
        "passed": False,
        "skipped": False,
        "profileRequested": PROFILE,
        "root": str(root),
        "workspace": str(workspace),
        "issues": [],
    }
    if not OPT_IN:
        summary.update({"skipped": True, "reason": "set GA_RUN_REAL_WORKFLOW_STRICT_SCHEMA_E2E=1 to run"})
        print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
        return 0
    try:
        if not check_profile(summary):
            summary["issues"].append("profile_mismatch")
            print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
            return 1
        store = WorkflowStore(root=str(root / "runs"))
        run = store.create_run(
            WorkflowRun(
                run_id="wf_strict_schema_e2e",
                session_id="workflow_strict_schema_e2e",
                script=STRICT_SCHEMA_SCRIPT,
                status="running",
            )
        )
        runner = NativeGPTChildAgentRunner(profile_name=PROFILE, max_tokens=1500, max_turns=3)
        started = time.time()
        try:
            outcome = WorkflowRuntime(
                store=store,
                runner=runner,
                scheduler_config=SchedulerConfig(max_concurrent=1, max_total=2),
                timeout_seconds=420.0,
            ).run(run, args={"workspacePath": str(workspace)})
            summary["runtimeOutcome"] = sanitize(
                getattr(outcome, "status", str(outcome)) if outcome is not None else None
            )
        except Exception as exc:
            summary["issues"].append(f"runtime_error:{type(exc).__name__}: {str(exc)[:400]}")
        summary["elapsedSeconds"] = round(time.time() - started, 1)

        loaded = store.load_run("wf_strict_schema_e2e")
        summary["runStatus"] = loaded.status
        jobs = [
            {
                "jobId": job.job_id,
                "label": job.metadata.get("label"),
                "status": job.status,
                "error": sanitize(job.error)[:300] if job.error else None,
                "schemaValidation": sanitize(
                    (job.metadata.get("schemaValidation") or {}).get("ok")
                ),
            }
            for job in loaded.jobs
        ]
        summary["jobs"] = jobs
        summary["workflowIssues"] = sanitize((loaded.metadata or {}).get("workflowIssues") or [])[:5]
        summary["passed"] = (
            loaded.status == "succeeded"
            and all(job["status"] == "succeeded" for job in jobs)
            and all(job["schemaValidation"] is True for job in jobs)
            and bool(jobs)
        )
    except Exception as exc:  # pragma: no cover - diagnostic path
        summary["issues"].append(f"{type(exc).__name__}: {str(exc)[:400]}")
    print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
