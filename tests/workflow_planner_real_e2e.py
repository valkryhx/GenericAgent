from __future__ import annotations

import contextlib
import io
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

from workflow_child_agent import NativeGPTChildAgentRunner
from workflow_models import WorkflowRun
from workflow_planner import WorkflowPlanner
from workflow_runtime import WorkflowRuntime
from workflow_scheduler import SchedulerConfig
from workflow_store import WorkflowStore

CONFIG_NAME = os.environ.get("GA_REAL_API_CONFIG", "native_oai_config")
EXPECTED_MODEL = os.environ.get("GA_REAL_API_EXPECTED_MODEL", "z.ai/glm-5.1")
EXPECTED_NAME = os.environ.get("GA_REAL_API_EXPECTED_NAME", "")
OPT_IN = os.environ.get("GA_RUN_REAL_WORKFLOW_PLANNER_E2E") == "1"

TASK_TEXT = "调研 GA Dynamic Workflow Planner / Compiler 的最小可验证设计是否符合预期"


def sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): sanitize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    if not isinstance(value, str):
        return value
    return value.replace("sk-", "sk-[REDACTED]-")


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]


def check_profile(summary: dict) -> bool:
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        from llmcore import reload_mykeys
        cfg = reload_mykeys()[0].get(CONFIG_NAME) or {}
    profile = {
        "configName": CONFIG_NAME,
        "name": cfg.get("name"),
        "model": cfg.get("model"),
        "apiMode": cfg.get("api_mode", "chat_completions"),
        "loadLogChars": len(captured.getvalue()),
    }
    summary["profile"] = profile
    summary["profileOk"] = profile["model"] == EXPECTED_MODEL and (not EXPECTED_NAME or profile["name"] == EXPECTED_NAME)
    return summary["profileOk"]


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="ga_workflow_planner_real_e2e_"))
    summary: dict[str, Any] = {
        "passed": False,
        "skipped": False,
        "configName": CONFIG_NAME,
        "expectedName": EXPECTED_NAME,
        "expectedModel": EXPECTED_MODEL,
        "root": str(root),
        "issues": [],
    }
    if not OPT_IN:
        summary.update({"skipped": True, "reason": "set GA_RUN_REAL_WORKFLOW_PLANNER_E2E=1 to run"})
        print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
        return 0
    try:
        if not check_profile(summary):
            summary["issues"].append("profile_mismatch")
            print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
            return 2

        planner = WorkflowPlanner()
        draft = planner.plan(
            TASK_TEXT,
            context={
                "sessionId": "workflow_planner_real_e2e",
                "constraints": ["不要读取 mykey.py", "不要读取 mykey.json", "不要读取 mcp.json", "不要提交"],
            },
        )
        store = WorkflowStore(root)
        run = store.create_run(WorkflowRun(
            run_id="wf_workflow_planner_real_e2e",
            session_id="workflow_planner_real_e2e",
            script=draft.script,
            status="running",
            metadata={"workflowDraft": draft.to_dict()},
        ))
        draft_ref = store.write_workflow_draft(run, draft)
        runner = NativeGPTChildAgentRunner(
            config_name=CONFIG_NAME,
            max_tokens=1024,
            max_turns=8,
        )
        runtime = WorkflowRuntime(
            store=store,
            runner=runner,
            scheduler_config=SchedulerConfig(max_concurrent=1, max_total=3),
            timeout_seconds=420.0,
        )
        start = time.time()
        outcome = runtime.run(run)
        elapsed = time.time() - start
        loaded = store.load_run(run.run_id)
        artifact_dir = Path(loaded.artifact_dir)
        progress_path = artifact_dir / "workflow-progress.json"
        final_path = artifact_dir / "final-result.json"
        draft_path = artifact_dir / draft_ref
        progress = load_json(progress_path) if progress_path.exists() else {}
        final_result = load_json(final_path) if final_path.exists() else {}
        persisted_draft = load_json(draft_path) if draft_path.exists() else {}
        jobs = loaded.jobs
        transcript_events = []
        for job in jobs:
            transcript_ref = job.metadata.get("transcriptRef")
            if transcript_ref:
                transcript_events.extend(read_jsonl(artifact_dir / transcript_ref))
        serialized_artifacts = json.dumps({"progress": progress, "final": final_result, "draft": persisted_draft}, ensure_ascii=False)
        summary.update({
            "elapsedSeconds": round(elapsed, 2),
            "status": loaded.status,
            "outcomeResult": sanitize(outcome.result),
            "artifactDir": str(artifact_dir),
            "draftRef": draft_ref,
            "draftExists": draft_path.exists(),
            "progressExists": progress_path.exists(),
            "finalWorkflowProgressRef": final_result.get("workflowProgressRef"),
            "classification": persisted_draft.get("classification"),
            "validation": persisted_draft.get("validation"),
            "phaseTitles": [phase.get("title") for phase in (persisted_draft.get("plan") or {}).get("phases") or []],
            "runtimePhases": outcome.phases,
            "jobLabels": [job.metadata.get("label") for job in jobs],
            "jobStatuses": [job.status for job in jobs],
            "transcriptEventTypes": sorted({event.get("type") for event in transcript_events if event.get("type")}),
            "scriptContainsMeta": "export const meta" in draft.script,
            "scriptContainsAgentLabel": "label: 'source-discovery'" in draft.script and "label: 'synthesis'" in draft.script,
            "artifactContainsSecretPattern": "sk-" in serialized_artifacts or "Bearer " in serialized_artifacts,
            "progressContainsTranscriptEvents": "transcriptEvents" in json.dumps(progress, ensure_ascii=False),
        })
        if loaded.status != "succeeded":
            summary["issues"].append("workflow_not_succeeded")
        if not draft_path.exists():
            summary["issues"].append("missing_workflow_draft")
        if final_result.get("workflowProgressRef") != "workflow-progress.json":
            summary["issues"].append("missing_workflow_progress_ref")
        if not progress_path.exists():
            summary["issues"].append("missing_workflow_progress")
        if (persisted_draft.get("classification") or {}).get("taskType") != "research":
            summary["issues"].append("classification_not_research")
        if not (persisted_draft.get("validation") or {}).get("ok"):
            summary["issues"].append("validation_not_ok")
        if summary["phaseTitles"] != ["Source Discovery", "Synthesis"]:
            summary["issues"].append("unexpected_plan_phases")
        if summary["runtimePhases"] != ["Source Discovery", "Synthesis"]:
            summary["issues"].append("unexpected_runtime_phases")
        if summary["jobLabels"] != ["source-discovery", "synthesis"]:
            summary["issues"].append("unexpected_job_labels")
        if not all(status == "succeeded" for status in summary["jobStatuses"]):
            summary["issues"].append("job_not_succeeded")
        if not summary["scriptContainsMeta"] or not summary["scriptContainsAgentLabel"]:
            summary["issues"].append("rendered_script_missing_required_dsl")
        if summary["artifactContainsSecretPattern"]:
            summary["issues"].append("artifact_contains_secret_pattern")
        if summary["progressContainsTranscriptEvents"]:
            summary["issues"].append("progress_contains_transcript_events")
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
