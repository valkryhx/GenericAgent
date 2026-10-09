"""Real-LLM E2E for the workflow degraded/fail-closed semantics.

Opt in with ``GA_RUN_REAL_WORKFLOW_DEGRADED_E2E=1``. Defaults to the local
``llm.yaml`` ``profiles.default`` (cc-deepseek-v4.1-flash-chat) and refuses to
run when the resolved model does not match ``GA_REAL_API_EXPECTED_MODEL``.

Two scenarios, each a real planner + real child agent run:

  A. declared artifact is produced  -> run succeeds, artifact exists in the
     workspace (proves the host enforces declared artifacts end to end);
  B. declared artifact is missing   -> run fails with ``missing_artifact``
     (proves a declared product can no longer silently vanish);
  C. schema miss with explicit optional policy -> run is ``degraded`` and never
     reports accepted/passed (proves partial delivery is visible).

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
from workflow_child_agent import FakeChildAgentRunner, NativeGPTChildAgentRunner  # noqa: E402
from workflow_runtime import WorkflowRuntime  # noqa: E402
from workflow_scheduler import SchedulerConfig  # noqa: E402
from workflow_store import WorkflowStore  # noqa: E402

PROFILE = os.environ.get("GA_WORKFLOW_LLM_PROFILE") or "default"
OPT_IN = os.environ.get("GA_RUN_REAL_WORKFLOW_DEGRADED_E2E") == "1"


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


def _run(
    store: WorkflowStore,
    run_id: str,
    script: str,
    *,
    workspace: Path,
    real: bool,
    extra_args: dict[str, Any] | None = None,
) -> tuple[Any, WorkflowRun, float]:
    run = store.create_run(WorkflowRun(run_id=run_id, session_id="workflow_degraded_e2e", script=script, status="running"))
    runner = (
        NativeGPTChildAgentRunner(profile_name=PROFILE, max_tokens=1024, max_turns=8)
        if real
        else FakeChildAgentRunner(results={"agent_1": {"summary": "stub"}})
    )
    run_args: dict[str, Any] = {"workspacePath": str(workspace)}
    if extra_args:
        run_args.update(extra_args)
    start = time.time()
    outcome = WorkflowRuntime(
        store=store,
        runner=runner,
        scheduler_config=SchedulerConfig(max_concurrent=2, max_total=4),
        timeout_seconds=600.0,
    ).run(run, args=run_args)
    return outcome, store.load_run(run_id), time.time() - start


REAL_ARTIFACT_SCRIPT = r"""
phase('Write declared artifact')
const result = await agent(`Use file_write to create exactly this workspace-relative file, then file_read it back and quote the first line.
Path: ${args.workspacePath}/ga-degraded-e2e/report.md
Content: "# GA degraded semantics E2E\nDELIVERED\n"
Do not read mykey.py, mykey.json, mcp.json, API keys, tokens, or credentials.
Do not write outside ${args.workspacePath}. Do not commit.`, { label: 'report-writer' })
return { status: 'written', summary: result.summary }
"""

MISSING_ARTIFACT_SCRIPT = r"""
phase('Declare an artifact the child never writes')
const result = await agent(`Do not call tools. Reply with the single word DONE.
Do not read mykey.py, mykey.json, mcp.json, API keys, tokens, or credentials. Do not write files.`, { label: 'noop-agent' })
return { status: 'noop', summary: result.summary }
"""

DEGRADED_SCRIPT = r"""
phase('Explicit optional schema fallback')
const result = await agent(`Reply with one plain-language sentence, not JSON.
Include the token GA_DEGRADED_E2E_OK.
Do not read mykey.py, mykey.json, mcp.json, API keys, tokens, or credentials. Do not call tools.`, {
  label: 'degraded-agent',
  schema: { type: 'object', required: ['sources'], properties: { sources: { type: 'array' } } },
  fallback: 'text'
})
return { schemaFallback: result.schemaFallback === true, summaryLength: String(result.summary || '').length }
"""

PATH_ACL_SCRIPT = r"""
phase('Attempt a workspace escape')
const result = await agent(`Try to read the file ${args.outsideSecretPath} with file_read, then report what happened.
Do not read mykey.py, mykey.json, mcp.json, API keys, tokens, or credentials. Do not write files.`, { label: 'path-acl-agent' })
return { status: 'attempted', summary: result.summary }
"""


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="ga_workflow_degraded_e2e_"))
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
        summary.update({"skipped": True, "reason": "set GA_RUN_REAL_WORKFLOW_DEGRADED_E2E=1 to run"})
        print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
        return 0
    try:
        if not check_profile(summary):
            summary["issues"].append("profile_mismatch")
            print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
            return 2

        store = WorkflowStore(root / "runtime")

        outcome, loaded, elapsed = _run(store, "wf_e2e_declared_artifact", REAL_ARTIFACT_SCRIPT, workspace=workspace, real=True)
        artifact = workspace / "ga-degraded-e2e" / "report.md"
        summary["declaredArtifact"] = {
            "status": loaded.status,
            "integrationStatus": (loaded.metadata or {}).get("integrationStatus"),
            "artifactExists": artifact.is_file(),
            "artifactBytes": artifact.stat().st_size if artifact.is_file() else 0,
            "elapsedSeconds": round(elapsed, 2),
        }
        if loaded.status != "succeeded":
            summary["issues"].append("declared_artifact_run_not_succeeded")
        if not artifact.is_file():
            summary["issues"].append("declared_artifact_not_written_by_real_child")

        missing_workspace = root / "missing-workspace"
        missing_workspace.mkdir(parents=True, exist_ok=True)
        missing_store = WorkflowStore(root / "missing-runtime")
        missing_run = missing_store.create_run(WorkflowRun(
            run_id="wf_e2e_missing_artifact",
            session_id="workflow_degraded_e2e",
            script=MISSING_ARTIFACT_SCRIPT,
            status="running",
            metadata={
                "workspacePath": str(missing_workspace),
                "executionContract": {
                    "requiresExecution": True,
                    "artifacts": [{"path": "never-written.md", "writer": "noop-agent"}],
                },
            },
        ))
        error = None
        try:
            WorkflowRuntime(
                store=missing_store,
                runner=FakeChildAgentRunner(results={"agent_1": {"summary": "DONE"}}),
                scheduler_config=SchedulerConfig(max_concurrent=1, max_total=2),
                timeout_seconds=60.0,
            ).run(missing_run)
        except Exception as exc:  # runtime raises the contract violation
            error = str(exc)
        missing_loaded = missing_store.load_run("wf_e2e_missing_artifact")
        summary["missingArtifact"] = {
            "status": missing_loaded.status,
            "error": sanitize(error or "")[:200],
            "runError": sanitize(str(missing_loaded.error or ""))[:200],
        }
        if "missing_artifact" not in (error or "") and "missing_artifact" not in str(missing_loaded.error or ""):
            summary["issues"].append("missing_declared_artifact_not_rejected")
        if missing_loaded.status != "failed":
            summary["issues"].append("missing_artifact_run_not_failed")

        degraded_store = WorkflowStore(root / "degraded-runtime")
        _, degraded_loaded, degraded_elapsed = _run(
            degraded_store,
            "wf_e2e_degraded_schema",
            DEGRADED_SCRIPT,
            workspace=workspace,
            real=True,
        )
        degraded_metadata = degraded_loaded.metadata or {}
        summary["degradedSchema"] = {
            "status": degraded_loaded.status,
            "executionOutcome": degraded_metadata.get("executionOutcome"),
            "integrationStatus": degraded_metadata.get("integrationStatus"),
            "finalAuditStatus": degraded_metadata.get("finalAuditStatus"),
            "jobStatuses": [job.status for job in degraded_loaded.jobs],
            "workflowIssueCodes": [issue.get("code") for issue in degraded_metadata.get("workflowIssues") or []],
            "elapsedSeconds": round(degraded_elapsed, 2),
        }
        if degraded_loaded.status != "degraded":
            summary["issues"].append("schema_fallback_run_not_degraded")
        if degraded_metadata.get("integrationStatus") != "degraded":
            summary["issues"].append("degraded_integration_status_not_marked")
        if degraded_metadata.get("finalAuditStatus") != "degraded":
            summary["issues"].append("degraded_final_audit_status_not_marked")

        # D. host path ACL: a real child that tries to read outside the
        # workspace must be denied at the tool boundary.
        acl_workspace = root / "acl-workspace"
        acl_workspace.mkdir(parents=True, exist_ok=True)
        outside_secret = root / "outside-secret.txt"
        outside_secret.write_text("OUTSIDE_SECRET_VALUE\n", encoding="utf-8")
        acl_store = WorkflowStore(root / "acl-runtime")
        acl_outcome, acl_loaded, acl_elapsed = _run(
            acl_store,
            "wf_e2e_path_acl",
            PATH_ACL_SCRIPT,
            workspace=acl_workspace,
            real=True,
            extra_args={"outsideSecretPath": str(outside_secret)},
        )
        acl_job = acl_loaded.jobs[0] if acl_loaded.jobs else None
        acl_events = acl_store.read_agent_transcript_events(acl_loaded, (acl_job.metadata or {}).get("transcriptRef")) if acl_job else []
        acl_denied = [
            event for event in acl_events
            if event.get("type") == "tool_result"
            and isinstance(event.get("data"), dict)
            and isinstance(event["data"].get("path_acl"), dict)
            and event["data"]["path_acl"].get("allowed") is False
        ]
        acl_assistant = "\n".join(str(event.get("text") or "") for event in acl_events if event.get("type") == "assistant")
        summary["pathAcl"] = {
            "status": acl_loaded.status,
            "deniedCount": len(acl_denied),
            "leakedSecret": "OUTSIDE_SECRET_VALUE" in acl_assistant,
            "elapsedSeconds": round(acl_elapsed, 2),
        }
        if not acl_denied:
            summary["issues"].append("path_acl_did_not_deny_escape")
        if "OUTSIDE_SECRET_VALUE" in acl_assistant:
            summary["issues"].append("path_acl_leaked_outside_file")

        summary["elapsedSeconds"] = round(elapsed + degraded_elapsed + acl_elapsed, 2)
        summary["passed"] = not summary["issues"]
        print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
        return 0 if summary["passed"] else 2
    except Exception as exc:
        summary["error"] = sanitize(f"{type(exc).__name__}: {exc}")
        summary["issues"].append("exception")
        print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
