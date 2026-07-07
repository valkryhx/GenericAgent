from __future__ import annotations

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

from prompt_guided_planner_real_e2e import RealPlannerClient, check_profile, labels, parse_json_object, phase_titles, sanitize
from workflow_child_agent import NativeGPTChildAgentRunner
from workflow_models import WorkflowRun
from workflow_planner import LLMWorkflowPlanner
from workflow_runtime import WorkflowRuntime
from workflow_scheduler import SchedulerConfig
from workflow_store import WorkflowStore

CONFIG_NAME = os.environ.get("GA_REAL_API_CONFIG", "native_oai_config")
EXPECTED_MODEL = os.environ.get("GA_REAL_API_EXPECTED_MODEL", "gpt-5.5")
EXPECTED_NAME = os.environ.get("GA_REAL_API_EXPECTED_NAME", "gpt-native")
OPT_IN = os.environ.get("GA_RUN_REAL_API_E2E") == "1"
REAL_MCP_OPT_IN = os.environ.get("GA_RUN_REAL_MCP_E2E") == "1"
OUTPUT_DETAIL = os.environ.get("GA_COMPLEX_WORKFLOW_DETAIL_OUT")

SECRET_PATTERNS = [
    re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE),
    re.compile(r"sk-[A-Za-z0-9_-]{12,}"),
    re.compile(r"sk-ant-[A-Za-z0-9_-]{12,}"),
    re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
]

COMPLEX_WORKFLOW_SCRIPT = r'''
phase('Real MCP Research');
log('start real MCP research agent');
const research = await agent(`You are the research agent for a GA complex workflow E2E.
You MUST call the real MCP tool mcp__tavily__tavily_search exactly once before final answer.
Call it with query: "2026 FIFA World Cup July 7 2026 matches results today".
Use max_results 3, search_depth "basic", topic "general" if those parameters are available.
After the MCP tool returns, provide a concise sanitized summary of what the search returned and include token GA_COMPLEX_MCP_RESEARCH_DONE.
Do not read mykey.py, mykey.json, mcp.json, API keys, tokens, or credentials. Do not write files.`, {label:'world-cup-real-mcp-research'});

phase('Coding With Existing Skill');
log('start coding agent with existing using-superpowers skill');
const coding = await agent(`You are the coding agent for a GA complex workflow E2E.
You MUST use GA's existing skills mechanism; do NOT create a temporary or fake skill.
First call load_skill with skill "using-superpowers".
Then do a safe coding task only under this temporary workspace: ${args.workspacePath}
Use file_write to create ${args.workspacePath}/ga_complex_skill_demo.py with a small pure Python function normalize_match_summary(text: str) -> str that strips extra whitespace and prefixes "World Cup summary: ".
Then use file_read to read back that exact file.
Final answer must mention that using-superpowers was loaded, the file was written and read, and include token GA_COMPLEX_CODING_SKILL_DONE.
Do not read mykey.py, mykey.json, mcp.json, API keys, tokens, or credentials. Do not write outside ${args.workspacePath}. Do not commit.`, {label:'using-superpowers-coding'});

phase('Synthesis');
log('start synthesis agent');
const synthesis = await agent(`Synthesize the complex workflow E2E results from these prior agents.
Research result: ${JSON.stringify(research)}
Coding result: ${JSON.stringify(coding)}
Return a concise verdict and include token GA_COMPLEX_SYNTHESIS_DONE.
Do not call tools, do not read secrets.`, {label:'complex-e2e-synthesis'});

return {
  marker: 'GA_COMPLEX_WORKFLOW_DONE',
  researchLength: String(research.summary || '').length,
  codingLength: String(coding.summary || '').length,
  synthesisLength: String(synthesis.summary || '').length
};
'''

PLANNER_TASK = (
    "设计一个复杂 GA workflow 真实 E2E：必须包含一个通过真实 MCP 搜索 ‘2026 FIFA World Cup July 7 2026 matches results today’ 的 research agent，"
    "一个通过现有 using-superpowers skill 完成安全临时编码任务的 coding agent，以及一个 synthesis agent。"
    "要求使用真实 gpt-5.5 planner；不要读取 mykey.py、mykey.json、mcp.json；不要提交；编码只允许写临时 workspace。"
)

class BoundedPlannerClient(RealPlannerClient):
    def complete(self, messages: list[dict]) -> dict:
        self.calls.append(messages)
        from llmcore import resolve_session

        session = resolve_session(self.config_name)
        prompt = messages[0]["content"] + """

复杂 E2E planner 附加要求：
- 只输出一个 JSON object，不要 Markdown。
- phases 总数 2-4；agents 总数 3-5。
- 必须出现 MCP research agent、using-superpowers coding agent、synthesis agent。
- 每个 agent prompt 必须包含：不要读取 mykey.py、mykey.json、mcp.json；不要提交。
- coding agent 必须明确 load_skill using-superpowers，而不是创建临时 skill。
"""
        raw = "".join(session.ask({"role": "user", "content": [{"type": "text", "text": prompt}]}))
        self.raw_outputs.append(raw)
        return parse_json_object(raw)


def discover_required_mcp() -> tuple[dict | None, dict]:
    if not REAL_MCP_OPT_IN:
        return None, {"skipped": True, "reason": "set GA_RUN_REAL_MCP_E2E=1 to require real MCP tool calling"}
    try:
        import mcp_runtime
        mcp_runtime.clear_mcp_cache()
        mcp_runtime.reset_mcp_manager()
        tools = mcp_runtime.discover_mcp_tools_cached(timeout=20)
    except Exception as exc:
        return None, {"error": f"{type(exc).__name__}: {exc}"}
    by_name = {(tool.get("function") or {}).get("name") or "": tool for tool in tools}
    names = sorted(name for name in by_name if name)
    schema = by_name.get("mcp__tavily__tavily_search")
    if not schema:
        return None, {"error": "mcp__tavily__tavily_search not discovered", "availableToolCount": len(names), "availableToolsSample": names[:30]}
    return schema, {"availableToolCount": len(names), "selectedTool": "mcp__tavily__tavily_search"}


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]


def has_secret(value: Any) -> bool:
    text = json.dumps(value, ensure_ascii=False, default=str)
    return any(pattern.search(text) for pattern in SECRET_PATTERNS)


def progress_for(run: WorkflowRun) -> dict:
    path = Path(run.artifact_dir or "") / "workflow-progress.json"
    return load_json(path)


def transcript_events_for(run: WorkflowRun, job) -> list[dict]:
    artifact_dir = Path(run.artifact_dir or "")
    result_path = artifact_dir / (job.result_ref or f"agents/{job.job_id}/result.json")
    result = load_json(result_path)
    ref = (job.metadata.get("transcriptRef") if job else None) or result.get("transcriptRef")
    return read_jsonl(artifact_dir / ref) if ref else []


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="ga_complex_workflow_real_e2e_"))
    workspace = root / "workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    summary: dict[str, Any] = {
        "passed": False,
        "skipped": False,
        "configName": CONFIG_NAME,
        "expectedName": EXPECTED_NAME,
        "expectedModel": EXPECTED_MODEL,
        "root": str(root),
        "workspace": str(workspace),
        "issues": [],
    }
    if not OPT_IN:
        summary.update({"skipped": True, "reason": "set GA_RUN_REAL_API_E2E=1 to run real complex workflow E2E"})
        print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
        return 0
    try:
        if not check_profile(summary):
            summary["issues"].append("profile_mismatch")
            print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
            return 2

        mcp_schema, discovery = discover_required_mcp()
        summary["mcpDiscovery"] = sanitize(discovery)
        if not mcp_schema:
            summary["issues"].append("mcp_tavily_search_unavailable")
            print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
            return 2

        planner_client = BoundedPlannerClient(CONFIG_NAME)
        planner = LLMWorkflowPlanner(client=planner_client, max_repair_attempts=1)
        draft = planner.plan(PLANNER_TASK, context={"constraints": ["使用真实 MCP", "使用现有 using-superpowers skill", "不要读取 mykey.py/mykey.json/mcp.json", "不要提交", "编码只写临时 workspace"]})
        summary["planner"] = {
            "taskType": draft.classification.get("taskType"),
            "validation": draft.validation,
            "phaseTitles": phase_titles(draft.plan),
            "labels": labels(draft.plan),
            "plannerCallCount": len(planner_client.calls),
        }
        label_text = " ".join(labels(draft.plan)).lower()
        if not draft.validation.get("ok"):
            summary["issues"].append("planner_validation_not_ok")
        if "mcp" not in label_text and "research" not in label_text:
            summary["issues"].append("planner_missing_mcp_research_semantics")
        if "skill" not in label_text and "coding" not in label_text and "superpowers" not in label_text:
            summary["issues"].append("planner_missing_skill_coding_semantics")

        store = WorkflowStore(root / "runtime")
        run = store.create_run(WorkflowRun(
            run_id="wf_complex_mcp_skill_coding_real",
            session_id="complex_workflow_real_e2e",
            script=COMPLEX_WORKFLOW_SCRIPT,
            status="running",
            metadata={"workflowName": "complex-mcp-skill-coding-real-e2e", "workflowTaskType": "mixed", "plannerMode": "prompt_guided+hand_checked_required_tools"},
        ))
        runner = NativeGPTChildAgentRunner(config_name=CONFIG_NAME, max_tokens=1024, max_turns=14)
        start = time.time()
        outcome = WorkflowRuntime(store=store, runner=runner, scheduler_config=SchedulerConfig(max_concurrent=2, max_total=6), timeout_seconds=900.0).run(run, args={"workspacePath": str(workspace)})
        elapsed = time.time() - start
        loaded = store.load_run(run.run_id)
        progress = progress_for(loaded)
        jobs = loaded.jobs
        all_events = []
        by_label = {}
        for job in jobs:
            events = transcript_events_for(loaded, job)
            all_events.extend(events)
            by_label[job.metadata.get("label") or job.job_id] = {
                "status": job.status,
                "toolCalls": [event.get("toolName") for event in events if event.get("type") == "tool_call"],
                "allowedTools": [event.get("toolName") for event in events if event.get("type") == "tool_allowed"],
                "deniedTools": [event.get("toolName") for event in events if event.get("type") == "tool_denied"],
            }
        tool_calls = [event.get("toolName") for event in all_events if event.get("type") == "tool_call"]
        tool_results = [event for event in all_events if event.get("type") == "tool_result"]
        denied = [event.get("toolName") for event in all_events if event.get("type") == "tool_denied"]
        skill_loaded = any(event.get("toolName") == "load_skill" and (event.get("data") or {}).get("status") == "success" and (event.get("data") or {}).get("name") == "using-superpowers" for event in tool_results)
        mcp_called = "mcp__tavily__tavily_search" in tool_calls
        mcp_returned = any(event.get("toolName") == "mcp__tavily__tavily_search" for event in tool_results)
        file_written = (workspace / "ga_complex_skill_demo.py").exists()
        file_content = (workspace / "ga_complex_skill_demo.py").read_text(encoding="utf-8") if file_written else ""
        coding_file_ok = "def normalize_match_summary" in file_content and "World Cup summary:" in file_content
        detail = {"run": loaded.to_dict(), "script": loaded.script, "events": [event.to_dict() for event in store.replay_events(loaded.run_id)], "draft": {"taskText": PLANNER_TASK, "classification": draft.classification, "plan": draft.plan, "validation": draft.validation, "context": draft.context}, "progress": progress or None}
        if OUTPUT_DETAIL:
            out = Path(OUTPUT_DETAIL)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(sanitize(detail), ensure_ascii=False, indent=2), encoding="utf-8")
        summary.update({
            "elapsedSeconds": round(elapsed, 2),
            "runId": loaded.run_id,
            "status": loaded.status,
            "runtimeStatus": outcome.run.status,
            "jobCount": len(jobs),
            "jobStatuses": [job.status for job in jobs],
            "jobsByLabel": sanitize(by_label),
            "toolCalls": tool_calls,
            "deniedTools": denied,
            "mcpCalled": mcp_called,
            "mcpReturned": mcp_returned,
            "usingSuperpowersLoaded": skill_loaded,
            "codingFileWritten": file_written,
            "codingFileOk": coding_file_ok,
            "progressEntryCount": len(progress.get("workflowProgress") or []) if isinstance(progress, dict) else 0,
            "progressEntries": sanitize(progress.get("workflowProgress") if isinstance(progress, dict) else []),
            "detailOut": OUTPUT_DETAIL,
            "result": sanitize(outcome.result),
        })
        if loaded.status != "succeeded" or outcome.run.status != "succeeded":
            summary["issues"].append("runtime_not_succeeded")
        if not jobs or not all(job.status == "succeeded" for job in jobs):
            summary["issues"].append("job_not_succeeded")
        if not mcp_called or not mcp_returned:
            summary["issues"].append("real_mcp_not_called_or_returned")
        if not skill_loaded:
            summary["issues"].append("using_superpowers_not_loaded")
        if not coding_file_ok:
            summary["issues"].append("coding_file_not_written_correctly")
        if denied:
            summary["issues"].append("tool_denied")
        if not progress.get("workflowProgress"):
            summary["issues"].append("missing_workflow_progress")
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
