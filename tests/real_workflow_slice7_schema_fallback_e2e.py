from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from sensitive_redaction import sanitize  # noqa: E402
from workflow_child_agent import NativeGPTChildAgentRunner  # noqa: E402
from workflow_models import WorkflowRun  # noqa: E402
from workflow_runtime import WorkflowRuntime  # noqa: E402
from workflow_scheduler import SchedulerConfig  # noqa: E402
from workflow_store import WorkflowStore  # noqa: E402


CONFIG_NAME = os.environ.get("GA_REAL_API_CONFIG", "native_oai_config")
EXPECTED_MODEL = os.environ.get("GA_REAL_API_EXPECTED_MODEL", "gpt-5.5")
EXPECTED_NAME = os.environ.get("GA_REAL_API_EXPECTED_NAME", "gpt-native")
OPT_IN = os.environ.get("GA_RUN_REAL_API_E2E") == "1"

SECRET_PATTERNS = [
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE),
    re.compile(r"sk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{12,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
]

SLICE7_SCRIPT = r"""
phase('Slice 7 Schema Fallback');
log('start real schema fallback agent');
const result = await agent(`You are running a GenericAgent Slice 7 real E2E check.
Return a concise plain-language sentence, not JSON and not a list of sources.
Include token GA_SLICE7_SCHEMA_REAL_DONE.
Do not read mykey.py, mykey.json, mcp.json, API keys, tokens, or credentials. Do not call tools. Do not write files.`, {
  label: 'schema-fallback-real',
  schema: {
    type: 'object',
    required: ['sources'],
    properties: { sources: { type: 'array' } }
  },
  fallback: 'text'
});
return {
  marker: 'GA_SLICE7_WORKFLOW_DONE',
  schemaFallback: result.schemaFallback === true,
  validationCode: result.schemaValidation && result.schemaValidation.code,
  fallbackApplied: result.schemaValidation && result.schemaValidation.fallbackApplied,
  fallback: result.schemaValidation && result.schemaValidation.fallback,
  issueCount: Array.isArray(result.schemaValidation && result.schemaValidation.issues) ? result.schemaValidation.issues.length : -1,
  summaryLength: String(result.summary || '').length,
  summaryIncludesToken: String(result.summary || '').includes('GA_SLICE7_SCHEMA_REAL_DONE')
};
"""


def check_profile(summary: dict[str, Any]) -> bool:
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        from llmcore import reload_mykeys

        cfg = reload_mykeys()[0].get(CONFIG_NAME) or {}
    profile = {
        "configName": CONFIG_NAME,
        "name": cfg.get("name"),
        "model": cfg.get("model"),
        "apiMode": cfg.get("api_mode") or cfg.get("apiMode") or "chat_completions",
        "loadLogChars": len(captured.getvalue()),
    }
    summary["profile"] = sanitize(profile)
    summary["profileOk"] = profile["model"] == EXPECTED_MODEL and (not EXPECTED_NAME or profile["name"] == EXPECTED_NAME)
    return bool(summary["profileOk"])


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def has_secret(value: Any) -> bool:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="ga_slice7_schema_real_e2e_"))
    summary: dict[str, Any] = {
        "passed": False,
        "skipped": False,
        "case": "slice7_schema_fallback_real_child_agent",
        "configName": CONFIG_NAME,
        "expectedName": EXPECTED_NAME,
        "expectedModel": EXPECTED_MODEL,
        "root": str(root),
        "issues": [],
    }
    if not OPT_IN:
        summary.update({"skipped": True, "reason": "set GA_RUN_REAL_API_E2E=1 to run real gpt-5.5 Slice 7 E2E"})
        print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
        return 0

    try:
        if not check_profile(summary):
            summary["issues"].append("profile_mismatch")
            print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
            return 2

        store = WorkflowStore(root / "runtime")
        run = store.create_run(
            WorkflowRun(
                run_id="wf_slice7_schema_fallback_real",
                session_id="slice7_schema_real_e2e",
                script=SLICE7_SCRIPT,
                status="running",
                metadata={"workflowName": "slice7-schema-fallback-real-e2e", "workflowTaskType": "review"},
            )
        )
        runner = NativeGPTChildAgentRunner(
            config_name=CONFIG_NAME,
            max_tokens=256,
            max_turns=4,
            enable_tools=False,
        )
        start = time.time()
        outcome = WorkflowRuntime(
            store=store,
            runner=runner,
            scheduler_config=SchedulerConfig(max_concurrent=1, max_total=2),
            timeout_seconds=300.0,
        ).run(run)
        elapsed = time.time() - start

        loaded = store.load_run(run.run_id)
        artifact_dir = Path(loaded.artifact_dir or "")
        progress = load_json(artifact_dir / "workflow-progress.json")
        final_result = load_json(artifact_dir / "final-result.json")
        events = [event.to_dict() for event in store.replay_events(loaded.run_id)]
        job = loaded.jobs[0] if loaded.jobs else None
        validation = (job.metadata.get("schemaValidation") if job else {}) or {}
        result_payload = (job.metadata.get("result") if job else {}) or {}
        workflow_issues = (loaded.metadata or {}).get("workflowIssues") or []
        progress_entries = progress.get("workflowProgress") or []
        progress_validation = (progress_entries[0].get("schemaValidation") if progress_entries else {}) or {}
        agent_result_artifact = load_json(artifact_dir / (job.result_ref or "")) if job and job.result_ref else {}
        transcript_ref = (job.metadata.get("transcriptRef") if job else None) or agent_result_artifact.get("transcriptRef")
        transcript_events = read_jsonl(artifact_dir / transcript_ref) if transcript_ref else []
        assistant_text = "\n".join(str(event.get("text") or "") for event in transcript_events if event.get("type") == "assistant")

        summary.update(
            {
                "elapsedSeconds": round(elapsed, 2),
                "runId": loaded.run_id,
                "status": loaded.status,
                "result": sanitize(outcome.result),
                "jobCount": len(loaded.jobs),
                "jobStatus": job.status if job else None,
                "jobLabel": job.metadata.get("label") if job else None,
                "schemaValidation": sanitize(validation),
                "workflowIssueCount": len(workflow_issues),
                "workflowIssueCodes": [issue.get("code") for issue in workflow_issues if isinstance(issue, dict)],
                "workflowIssueFallbackApplied": [issue.get("fallbackApplied") for issue in workflow_issues if isinstance(issue, dict)],
                "resultSchemaFallback": result_payload.get("schemaFallback"),
                "progressStatus": progress.get("status"),
                "progressEntryCount": len(progress_entries),
                "progressValidation": sanitize(progress_validation),
                "finalStatus": final_result.get("status"),
                "finalWorkflowIssueCount": len(final_result.get("workflowIssues") or []),
                "agentResultPayloadKeys": sorted((agent_result_artifact.get("payload") or {}).keys()),
                "workflowIssueEventCount": len([event for event in events if event.get("type") == "workflow_issue"]),
                "assistantTextLength": len(assistant_text),
                "assistantIncludedRequestedToken": "GA_SLICE7_SCHEMA_REAL_DONE" in assistant_text,
            }
        )

        if loaded.status != "succeeded" or outcome.run.status != "succeeded":
            summary["issues"].append("runtime_not_succeeded")
        if not job or job.status != "succeeded":
            summary["issues"].append("job_not_succeeded")
        if validation.get("code") != "schema_validation_failed":
            summary["issues"].append("missing_schema_validation_failed_code")
        if validation.get("fallback") != "text" or validation.get("fallbackApplied") is not True:
            summary["issues"].append("schema_text_fallback_not_applied")
        if result_payload.get("schemaFallback") is not True:
            summary["issues"].append("result_missing_schemaFallback")
        if len(workflow_issues) != 1:
            summary["issues"].append("workflow_issue_count_not_one")
        else:
            issue = workflow_issues[0]
            if issue.get("code") != "schema_validation_failed" or issue.get("fallbackApplied") is not True:
                summary["issues"].append("workflow_issue_contract_mismatch")
            if issue.get("jobId") != (job.job_id if job else None) or issue.get("agentLabel") != "schema-fallback-real":
                summary["issues"].append("workflow_issue_job_identity_mismatch")
        if progress.get("workflowIssues") != workflow_issues:
            summary["issues"].append("progress_workflow_issues_mismatch")
        if progress_validation != validation:
            summary["issues"].append("progress_schema_validation_mismatch")
        if final_result.get("workflowIssues") != workflow_issues:
            summary["issues"].append("final_workflow_issues_mismatch")
        if not isinstance(final_result.get("result"), dict) or final_result["result"].get("schemaFallback") is not True:
            summary["issues"].append("final_result_missing_schema_fallback")
        if not any(event.get("type") == "workflow_issue" for event in events):
            summary["issues"].append("missing_workflow_issue_event")
        if len(assistant_text.strip()) == 0:
            summary["issues"].append("missing_real_assistant_transcript")
        if has_secret(summary):
            summary["issues"].append("secret_pattern_detected_in_summary")

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
