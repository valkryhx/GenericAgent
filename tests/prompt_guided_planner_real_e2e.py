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

from workflow_child_agent import FakeChildAgentRunner
from workflow_models import WorkflowRun
from workflow_planner import LLMWorkflowPlanner
from workflow_runtime import WorkflowRuntime
from workflow_scheduler import SchedulerConfig
from workflow_store import WorkflowStore

CONFIG_NAME = (
    os.environ.get("GA_WORKFLOW_LLM_PROFILE")
    or os.environ.get("GA_REAL_API_PROFILE")
    or os.environ.get("GA_REAL_API_CONFIG")
    or "grok"
)
if CONFIG_NAME.endswith("_config") or CONFIG_NAME.startswith("native_"):
    CONFIG_NAME = "grok"
EXPECTED_MODEL = os.environ.get("GA_REAL_API_EXPECTED_MODEL", "grok-4.5")
EXPECTED_NAME = os.environ.get("GA_REAL_API_EXPECTED_NAME", "grok")
OPT_IN = os.environ.get("GA_RUN_REAL_PROMPT_PLANNER_E2E") == "1"

SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_-]{16,}|Bearer\s+[A-Za-z0-9._~+/=-]{24,})")

SCENARIOS = [
    {
        "name": "research",
        "task": "调研一个技术方案的可信度、证据来源、矛盾点和落地风险，只需要生成 workflow plan，不要执行真实网络搜索。",
        "expectedTaskType": "research",
        "requiredPhasesAny": ["Source", "Discovery", "Credibility", "Evidence", "Synthesis"],
        "requiredLabelsAny": ["source", "credibility", "synthesis", "evidence"],
    },
    {
        "name": "review",
        "task": "这不是调研任务。请生成 review workflow plan：全面审查一个 PR 的安全、性能、测试缺口和回归风险，并验证 findings 是否真实。taskType 必须是 review。",
        "expectedTaskType": "review",
        "requiredPhasesAny": ["Review", "Verify", "Synthesis", "Report", "Dimension", "Analysis", "Finding", "Validation"],
        "requiredLabelsAny": ["security", "performance", "test", "regression", "verify"],
    },
    {
        "name": "coding",
        "task": "实现 workflow controller 的 planned run 入口，必须遵守 TDD：先理解，再写 failing tests，再实现，再验证。",
        "expectedTaskType": "coding",
        "requiredPhasesAny": ["Understand", "Tests", "Implementation", "Verification", "Summary"],
        "requiredLabelsAny": ["understand", "test", "implement", "verify"],
    },
    {
        "name": "planning",
        "task": "规划一个跨前端、后端和 workflow runtime 的 prompt-guided planner feature，先调研上下文、比较方案、审查风险，再给实施计划，不要直接写代码。",
        "expectedTaskType": "planning",
        "expectedTaskTypes": ["planning", "mixed"],
        "requiredPhasesAny": ["Context", "Discovery", "Design", "Alternatives", "Risk", "Implementation Plan"],
        "requiredLabelsAny": ["context", "design", "risk", "plan"],
    },
]


class RealPlannerClient:
    def __init__(self, config_name: str):
        # config_name is llm.yaml profile name (e.g. grok)
        self.config_name = config_name
        self.profile_name = config_name
        self.calls: list[list[dict]] = []
        self.raw_outputs: list[str] = []

    def complete(self, messages: list[dict]) -> dict:
        from workflow_llm import binding_from_profile, make_session

        session = make_session(binding_from_profile(self.profile_name or self.config_name))
        self.calls.append(messages)
        prompt = messages[0]["content"] + """

硬性输出要求：
- 只输出一个 JSON object。
- 不要 Markdown。
- 不要解释。
- 不要输出 JavaScript；只输出 WorkflowPlan JSON。
"""
        raw = "".join(session.ask({"role": "user", "content": [{"type": "text", "text": prompt}]}))
        self.raw_outputs.append(raw)
        return parse_json_object(raw)


class FailingPlannerClient:
    def __init__(self):
        self.calls: list[list[dict]] = []

    def complete(self, messages: list[dict]) -> dict:
        self.calls.append(messages)
        raise RuntimeError("intentional planner failure for fallback E2E")


def parse_json_object(raw: str) -> dict:
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            return json.loads(text[start : end + 1])
        raise


def sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): sanitize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    if not isinstance(value, str):
        return value
    return SECRET_RE.sub("[REDACTED_SECRET]", value)


def check_profile(summary: dict) -> bool:
    from workflow_llm import binding_from_profile

    try:
        binding = binding_from_profile(CONFIG_NAME)
        profile = {
            "configName": CONFIG_NAME,
            "profileName": binding.profile_name,
            "name": binding.profile_name,
            "model": binding.model_id,
            "apiMode": "yaml",
            "loadLogChars": 0,
        }
        summary["profile"] = profile
        summary["profileOk"] = profile["model"] == EXPECTED_MODEL and (
            not EXPECTED_NAME or profile["name"] == EXPECTED_NAME
        )
        return bool(summary["profileOk"])
    except Exception as exc:
        summary["profile"] = {"error": f"{type(exc).__name__}: {exc}", "configName": CONFIG_NAME}
        summary["profileOk"] = False
        return False


def phase_titles(plan: dict) -> list[str]:
    return [str(phase.get("title") or "") for phase in plan.get("phases") or []]


def labels(plan: dict) -> list[str]:
    result = []
    for phase in plan.get("phases") or []:
        for agent in phase.get("agents") or []:
            result.append(str(agent.get("label") or ""))
    return result


def contains_any(values: list[str], needles: list[str]) -> bool:
    haystack = " ".join(values).lower()
    return any(needle.lower() in haystack for needle in needles)


def run_fake_runtime(root: Path, scenario_name: str, script: str) -> dict:
    store = WorkflowStore(root)
    run = store.create_run(WorkflowRun(run_id=f"wf_prompt_planner_{scenario_name}", session_id="prompt_planner_real_e2e", script=script, status="running"))
    outcome = WorkflowRuntime(
        store=store,
        runner=FakeChildAgentRunner(),
        scheduler_config=SchedulerConfig(max_concurrent=4, max_total=32),
        timeout_seconds=8.0,
    ).run(run)
    loaded = store.load_run(run.run_id)
    return {
        "status": loaded.status,
        "runtimePhases": outcome.phases,
        "jobLabels": [job.metadata.get("label") for job in loaded.jobs],
        "jobStatuses": [job.status for job in loaded.jobs],
    }


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="ga_prompt_guided_planner_real_e2e_"))
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
        summary.update({"skipped": True, "reason": "set GA_RUN_REAL_PROMPT_PLANNER_E2E=1 to run"})
        print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
        return 0
    try:
        if not check_profile(summary):
            summary["issues"].append("profile_mismatch")
            print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
            return 2

        planner_client = RealPlannerClient(CONFIG_NAME)
        planner = LLMWorkflowPlanner(client=planner_client, max_repair_attempts=1)
        start = time.time()
        for scenario in SCENARIOS:
            draft = planner.plan(
                scenario["task"],
                context={"constraints": ["不要读取 mykey.py", "不要读取 mykey.json", "不要读取 mcp.json", "不要提交"]},
            )
            runtime = run_fake_runtime(root, scenario["name"], draft.script) if draft.validation.get("ok") else {}
            scenario_summary = {
                "name": scenario["name"],
                "taskType": draft.classification.get("taskType"),
                "validation": draft.validation,
                "plannerMode": draft.context.get("plannerMode"),
                "phaseTitles": phase_titles(draft.plan),
                "labels": labels(draft.plan),
                "scriptContainsMeta": "export const meta" in draft.script,
                "runtime": runtime,
            }
            summary["scenarios"].append(scenario_summary)
            expected_types = scenario.get("expectedTaskTypes") or [scenario["expectedTaskType"]]
            if draft.classification.get("taskType") not in expected_types:
                summary["issues"].append(f"{scenario['name']}:unexpected_task_type")
            if not draft.validation.get("ok"):
                summary["issues"].append(f"{scenario['name']}:validation_not_ok")
            if not contains_any(scenario_summary["phaseTitles"], scenario["requiredPhasesAny"]):
                summary["issues"].append(f"{scenario['name']}:missing_expected_phase_semantics")
            if not contains_any(scenario_summary["labels"], scenario["requiredLabelsAny"]):
                summary["issues"].append(f"{scenario['name']}:missing_expected_label_semantics")
            if not scenario_summary["scriptContainsMeta"]:
                summary["issues"].append(f"{scenario['name']}:script_missing_meta")
            if runtime.get("status") != "succeeded":
                summary["issues"].append(f"{scenario['name']}:runtime_not_succeeded")
            if not all(status == "succeeded" for status in runtime.get("jobStatuses", [])):
                summary["issues"].append(f"{scenario['name']}:job_not_succeeded")
            if scenario["name"] == "coding":
                script = draft.script
                if "label: 'write" in script and "label: 'implement" in script and not script.index("label: 'write") < script.index("label: 'implement"):
                    summary["issues"].append("coding:tests_not_before_implementation")
                if "coding_tests_parallel_implementation" in {issue.get("code") for issue in draft.validation.get("issues", [])}:
                    summary["issues"].append("coding:parallel_tests_implementation_not_repaired")

        fallback_planner = LLMWorkflowPlanner(client=FailingPlannerClient())
        fallback_draft = fallback_planner.plan("调研 fallback 是否可用", context={"constraints": ["不要读取 mykey.py", "不要提交"]})
        summary["fallback"] = {
            "plannerMode": fallback_draft.context.get("plannerMode"),
            "validationMode": fallback_draft.validation.get("mode"),
            "ok": fallback_draft.validation.get("ok"),
            "taskType": fallback_draft.classification.get("taskType"),
        }
        if fallback_draft.validation.get("mode") != "fallback_deterministic" or not fallback_draft.validation.get("ok"):
            summary["issues"].append("fallback_not_working")

        serialized = json.dumps(summary, ensure_ascii=False)
        if SECRET_RE.search(serialized):
            summary["issues"].append("summary_contains_secret_pattern")
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
