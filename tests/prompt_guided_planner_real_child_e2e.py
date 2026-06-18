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

from prompt_guided_planner_real_e2e import RealPlannerClient, parse_json_object, sanitize, check_profile, phase_titles, labels
from workflow_child_agent import NativeGPTChildAgentRunner
from workflow_models import WorkflowRun
from workflow_planner import LLMWorkflowPlanner
from workflow_runtime import WorkflowRuntime
from workflow_scheduler import SchedulerConfig
from workflow_store import WorkflowStore

CONFIG_NAME = os.environ.get("GA_REAL_API_CONFIG", "native_oai_config")
EXPECTED_MODEL = os.environ.get("GA_REAL_API_EXPECTED_MODEL", "z.ai/glm-5.1")
EXPECTED_NAME = os.environ.get("GA_REAL_API_EXPECTED_NAME", "")
OPT_IN = os.environ.get("GA_RUN_REAL_PROMPT_PLANNER_REAL_CHILD_E2E") == "1"

SCENARIOS = [
    {
        "name": "prompt_planner_code_review",
        "task": "真实场景：审查当前 GA prompt-guided planner 实现。请生成 review workflow plan，围绕 workflow_planner.py、tests/test_workflow_prompt_guided_planner.py、tests/prompt_guided_planner_real_e2e.py，从 correctness、TDD 顺序、安全边界/secret hygiene 三个角度审查，并综合 findings。允许子智能体使用安全只读工具读取这些指定文件；不要修改文件，不要提交；不要读取 mykey.py、mykey.json、mcp.json。taskType 必须是 review。最多 4 个 agent。",
        "expectedTypes": ["review"],
        "maxTotal": 5,
        "requiredLabelAny": ["correctness", "tdd", "security", "secret", "review", "verify"],
    },
    {
        "name": "workflow_docs_consistency_research",
        "task": "真实场景：检查文档与代码是否一致。请生成 research 或 planning workflow plan，读取 docs/GA_workflow_defect_optimization_plan.md、docs/claude_code_dynamic_workflow_reference.md、workflow_planner.py，分析 Prompt-guided planner 的文档描述是否与当前代码一致，并给出缺口。允许子智能体使用安全只读工具读取这些指定文件；不要修改文件，不要提交；不要读取 mykey.py、mykey.json、mcp.json。最多 4 个 agent。",
        "expectedTypes": ["planning", "mixed", "research"],
        "maxTotal": 5,
        "requiredLabelAny": ["doc", "code", "consistency", "research", "gap", "plan"],
    },
]


class BoundedRealPlannerClient(RealPlannerClient):
    def complete(self, messages: list[dict]) -> dict:
        self.calls.append(messages)
        from llmcore import resolve_session

        session = resolve_session(self.config_name)
        prompt = messages[0]["content"] + """

真实 child agent E2E 专用硬性要求：
- 只输出一个 JSON object，不要 Markdown，不要解释。
- 不要输出 JavaScript；只输出 WorkflowPlan JSON。
- phases 总数最多 3 个。
- agents 总数最多 4 个。
- 每个 agent.prompt 必须包含：不要读取 mykey.py、mykey.json、mcp.json；不要提交；只允许安全只读工具；不要修改文件。
- 可以要求 agent 使用安全只读工具读取任务中明确列出的文件。
- 不要要求 agent 写文件、执行破坏性命令、访问凭据文件或提交代码。
"""
        raw = "".join(session.ask({"role": "user", "content": [{"type": "text", "text": prompt}]}))
        self.raw_outputs.append(raw)
        return parse_json_object(raw)


def run_real_child_runtime(root: Path, scenario_name: str, script: str, max_total: int) -> tuple[dict, WorkflowStore, WorkflowRun]:
    store = WorkflowStore(root)
    run = store.create_run(WorkflowRun(run_id=f"wf_real_child_{scenario_name}", session_id="prompt_planner_real_child_e2e", script=script, status="running"))
    runner = NativeGPTChildAgentRunner(
        config_name=CONFIG_NAME,
        max_tokens=768,
        max_turns=4,
        enable_tools=True,
    )
    outcome = WorkflowRuntime(
        store=store,
        runner=runner,
        scheduler_config=SchedulerConfig(max_concurrent=1, max_total=max_total),
        timeout_seconds=720.0,
    ).run(run)
    loaded = store.load_run(run.run_id)
    return {
        "status": loaded.status,
        "runtimePhases": outcome.phases,
        "jobLabels": [job.metadata.get("label") for job in loaded.jobs],
        "jobStatuses": [job.status for job in loaded.jobs],
        "resultKeys": sorted((outcome.result or {}).keys()) if isinstance(outcome.result, dict) else [],
        "artifactDir": loaded.artifact_dir,
    }, store, loaded


def load_progress(run: WorkflowRun) -> dict:
    path = Path(run.artifact_dir) / "workflow-progress.json"
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="ga_prompt_guided_planner_real_child_e2e_"))
    summary: dict[str, Any] = {
        "passed": False,
        "skipped": False,
        "configName": CONFIG_NAME,
        "expectedName": EXPECTED_NAME,
        "expectedModel": EXPECTED_MODEL,
        "root": str(root),
        "issues": [],
        "scenarios": [],
    }
    if not OPT_IN:
        summary.update({"skipped": True, "reason": "set GA_RUN_REAL_PROMPT_PLANNER_REAL_CHILD_E2E=1 to run"})
        print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
        return 0
    try:
        if not check_profile(summary):
            summary["issues"].append("profile_mismatch")
            print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
            return 2

        planner_client = BoundedRealPlannerClient(CONFIG_NAME)
        planner = LLMWorkflowPlanner(client=planner_client, max_repair_attempts=1)
        start = time.time()
        for scenario in SCENARIOS:
            draft = planner.plan(
                scenario["task"],
                context={"constraints": ["不要读取 mykey.py", "不要读取 mykey.json", "不要读取 mcp.json", "不要提交", "只允许安全只读工具", "不要修改文件"]},
            )
            runtime = {}
            progress = {}
            if draft.validation.get("ok"):
                runtime, store, loaded = run_real_child_runtime(root, scenario["name"], draft.script, scenario["maxTotal"])
                progress = load_progress(loaded)
            scenario_summary = {
                "name": scenario["name"],
                "taskType": draft.classification.get("taskType"),
                "validation": draft.validation,
                "plannerMode": draft.context.get("plannerMode"),
                "phaseTitles": phase_titles(draft.plan),
                "labels": labels(draft.plan),
                "runtime": runtime,
                "progressEntries": [
                    {
                        "label": item.get("label"),
                        "state": item.get("state"),
                        "toolCalls": item.get("toolCalls"),
                        "resultPreview": item.get("resultPreview"),
                    }
                    for item in progress.get("workflowProgress", [])
                ],
            }
            summary["scenarios"].append(scenario_summary)
            if draft.classification.get("taskType") not in scenario["expectedTypes"]:
                summary["issues"].append(f"{scenario['name']}:unexpected_task_type")
            if not draft.validation.get("ok"):
                summary["issues"].append(f"{scenario['name']}:validation_not_ok")
            if len(labels(draft.plan)) < 2:
                summary["issues"].append(f"{scenario['name']}:not_multi_agent")
            if len(labels(draft.plan)) > scenario["maxTotal"]:
                summary["issues"].append(f"{scenario['name']}:too_many_agents")
            if runtime.get("status") != "succeeded":
                summary["issues"].append(f"{scenario['name']}:runtime_not_succeeded")
            if not all(status == "succeeded" for status in runtime.get("jobStatuses", [])):
                summary["issues"].append(f"{scenario['name']}:job_not_succeeded")
            if not any(needle in " ".join(labels(draft.plan)).lower() for needle in scenario["requiredLabelAny"]):
                summary["issues"].append(f"{scenario['name']}:missing_expected_label_semantics")
            if not progress.get("workflowProgress"):
                summary["issues"].append(f"{scenario['name']}:missing_progress")
            tool_call_count = sum(len(item.get("toolCalls") or []) for item in progress.get("workflowProgress", []))
            scenario_summary["toolCallCount"] = tool_call_count
            if tool_call_count < 1:
                summary["issues"].append(f"{scenario['name']}:no_real_child_tool_calls")

        summary["elapsedSeconds"] = round(time.time() - start, 2)
        summary["plannerCallCount"] = len(planner_client.calls)
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
