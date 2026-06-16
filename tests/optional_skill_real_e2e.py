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
from workflow_runtime import WorkflowRuntime
from workflow_scheduler import SchedulerConfig
from workflow_store import WorkflowStore

CONFIG_NAME = os.environ.get("GA_REAL_API_CONFIG", "native_oai_config")
EXPECTED_NAME = os.environ.get("GA_REAL_API_EXPECTED_NAME", "gpt-native")
EXPECTED_MODEL = os.environ.get("GA_REAL_API_EXPECTED_MODEL", "gpt-5.4")
OPT_IN = os.environ.get("GA_RUN_REAL_WORKFLOW_E2E") == "1"

WORKFLOW_SCRIPT = r'''
phase('Optional Skill E2E')
log('start optional skill progress e2e')

const result = await agent(`这是 GA workflow optional skill awareness 的真实 API E2E 验证任务。

请完成以下动作：
1. 如果 load_skill 工具可用，请调用 load_skill 加载 test-driven-development skill。
2. 简要说明你已经加载了该 skill，并输出标记 GA_OPTIONAL_SKILL_E2E_DONE。
3. 不要读取 mykey.py、mykey.json、mcp.json 或任何凭据文件。
4. 不要修改仓库源码。`, {
  label: 'optional-skill-real-agent'
})

return {
  marker: 'GA_OPTIONAL_SKILL_E2E_DONE',
  summary: result.summary || result.text || result
}
'''


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
    summary["profileOk"] = profile["name"] == EXPECTED_NAME and profile["model"] == EXPECTED_MODEL
    return summary["profileOk"]


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="ga_optional_skill_real_e2e_"))
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
        summary.update({"skipped": True, "reason": "set GA_RUN_REAL_WORKFLOW_E2E=1 to run"})
        print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
        return 0
    try:
        if not check_profile(summary):
            summary["issues"].append("profile_mismatch")
            print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
            return 2

        store = WorkflowStore(root)
        run = store.create_run(WorkflowRun(
            run_id="wf_optional_skill_real_e2e",
            session_id="optional_skill_real_e2e",
            script=WORKFLOW_SCRIPT,
            status="running",
        ))
        runner = NativeGPTChildAgentRunner(
            config_name=CONFIG_NAME,
            max_tokens=1536,
            max_turns=8,
        )
        runtime = WorkflowRuntime(
            store=store,
            runner=runner,
            scheduler_config=SchedulerConfig(max_concurrent=1, max_total=2),
            timeout_seconds=420.0,
        )
        start = time.time()
        outcome = runtime.run(run)
        elapsed = time.time() - start
        loaded = store.load_run(run.run_id)
        artifact_dir = Path(loaded.artifact_dir)
        progress_path = artifact_dir / "workflow-progress.json"
        final_path = artifact_dir / "final-result.json"
        progress = load_json(progress_path) if progress_path.exists() else {}
        final_result = load_json(final_path) if final_path.exists() else {}
        job = loaded.jobs[0] if loaded.jobs else None
        transcript_ref = job.metadata.get("transcriptRef") if job else None
        transcript_events = read_jsonl(artifact_dir / transcript_ref) if transcript_ref else []
        progress_entry = (progress.get("workflowProgress") or [{}])[0]
        tool_calls = [event.get("toolName") for event in transcript_events if event.get("type") == "tool_call"]
        skill_results = [event.get("data") for event in transcript_events if event.get("type") == "tool_result" and event.get("toolName") == "load_skill"]
        serialized_progress = json.dumps(progress, ensure_ascii=False)
        summary.update({
            "elapsedSeconds": round(elapsed, 2),
            "status": loaded.status,
            "outcomeResult": sanitize(outcome.result),
            "artifactDir": str(artifact_dir),
            "finalWorkflowProgressRef": final_result.get("workflowProgressRef"),
            "progressExists": progress_path.exists(),
            "jobStatus": job.status if job else None,
            "jobLabel": job.metadata.get("label") if job else None,
            "toolCalls": tool_calls,
            "loadedSkills": progress_entry.get("loadedSkills"),
            "skillToolCalls": progress_entry.get("skillToolCalls"),
            "skillLoadEvents": progress_entry.get("skillLoadEvents"),
            "loadSkillAvailable": (progress_entry.get("capability") or {}).get("loadSkillAvailable"),
            "skillResultCount": len(skill_results),
            "progressContainsSkillContent": "Base directory for this skill" in serialized_progress or "FULL SKILL" in serialized_progress,
            "markerFound": "GA_OPTIONAL_SKILL_E2E_DONE" in json.dumps(final_result, ensure_ascii=False),
        })
        if loaded.status != "succeeded":
            summary["issues"].append("workflow_not_succeeded")
        if final_result.get("workflowProgressRef") != "workflow-progress.json":
            summary["issues"].append("missing_workflow_progress_ref")
        if not progress_path.exists():
            summary["issues"].append("missing_workflow_progress")
        if progress_entry.get("skillToolCalls", 0) < 1:
            summary["issues"].append("skill_tool_call_not_recorded")
        if "test-driven-development" not in (progress_entry.get("loadedSkills") or []):
            summary["issues"].append("loaded_skill_missing_from_progress")
        if not progress_entry.get("skillLoadEvents"):
            summary["issues"].append("skill_load_events_missing")
        if summary["progressContainsSkillContent"]:
            summary["issues"].append("progress_leaked_skill_content")
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
