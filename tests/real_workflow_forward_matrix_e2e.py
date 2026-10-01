"""Opt-in real forward-testing matrix for GA dynamic workflow policy.

Run with:
    GA_RUN_REAL_FORWARD_MATRIX=1 python tests/real_workflow_forward_matrix_e2e.py

Defaults to the deepseek-v4.1-flash llm.yaml profile. The matrix exercises
real planner + runtime wiring for direct, workflow, delegated, fallback,
approval and eval-contract paths. It prints only statuses/metadata, never
prompts, transcripts or credentials.
"""

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

from workflow_child_agent import FakeChildAgentRunner
from workflow_controller import WorkflowController
from workflow_models import WorkflowRun
from workflow_planner import LLMWorkflowPlanner, WorkflowPlanner
from workflow_policy import build_forward_test_matrix
from workflow_runtime import WorkflowRuntime
from workflow_scheduler import SchedulerConfig
from workflow_store import WorkflowStore

PROFILE = (
    os.environ.get("GA_FORWARD_MATRIX_PROFILE")
    or os.environ.get("GA_WORKFLOW_LLM_PROFILE")
    or "deepseek-v4.1-flash"
)
OPT_IN = os.environ.get("GA_RUN_REAL_FORWARD_MATRIX") == "1"
SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_-]{16,}|Bearer\s+[A-Za-z0-9._~+/=-]{24,})")


def sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): sanitize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    if not isinstance(value, str):
        return value
    return SECRET_RE.sub("[REDACTED_SECRET]", value)


class RealPlannerClient:
    def __init__(self, profile: str):
        self.profile = profile
        self.call_count = 0

    def complete(self, messages: list[dict]) -> dict:
        from workflow_llm import binding_from_profile, make_session

        session = make_session(binding_from_profile(self.profile))
        self.call_count += 1
        prompt = messages[0]["content"] + "\n\nHard requirements:\n- Output exactly one JSON object.\n- No Markdown, no prose, no JavaScript.\n"
        raw = "".join(str(chunk) for chunk in session.ask({"role": "user", "content": [{"type": "text", "text": prompt}]}))
        return _parse_json_object(raw)


class FailingPlannerClient:
    def complete(self, messages: list[dict]) -> dict:
        raise RuntimeError("intentional planner failure for fallback matrix case")


def _parse_json_object(raw: str) -> dict:
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


def _run_script(script: str, *, run_id: str, timeout: float = 20.0) -> dict:
    root = Path(tempfile.mkdtemp(prefix=f"ga_forward_{run_id}_"))
    store = WorkflowStore(root)
    run = store.create_run(
        WorkflowRun(run_id=run_id, session_id="forward_matrix", script=script, status="running")
    )
    outcome = WorkflowRuntime(
        store=store,
        runner=FakeChildAgentRunner(),
        scheduler_config=SchedulerConfig(max_concurrent=4, max_total=32),
        timeout_seconds=timeout,
    ).run(run, args={"workspacePath": str(root)})
    loaded = store.load_run(run_id)
    return {
        "status": loaded.status,
        "phases": outcome.phases,
        "jobLabels": [job.metadata.get("label") for job in loaded.jobs],
        "jobWaves": [job.metadata.get("wave") for job in loaded.jobs],
        "jobStatuses": [job.status for job in loaded.jobs],
        "integrationStatus": loaded.metadata.get("integrationStatus"),
        "finalAuditStatus": loaded.metadata.get("finalAuditStatus"),
    }


def _case_direct(planner_client) -> dict:
    planner = LLMWorkflowPlanner(client=planner_client, max_repair_attempts=1)
    draft = planner.plan("回答一个问题：3 加 4 等于几？不要创建 workflow，直接给结果。")
    return {
        "name": "direct",
        "validationOk": bool(draft.validation.get("ok")),
        "mode": draft.plan.get("mode"),
        "riskLevel": draft.plan.get("riskLevel"),
        "expected": "single-turn",
    }


def _case_workflow(planner_client) -> dict:
    planner = LLMWorkflowPlanner(client=planner_client, max_repair_attempts=1)
    draft = planner.plan(
        "调研 GA workflow 的依赖调度设计，先收集上下文，再综合结论，不要执行真实网络搜索。",
        context={"constraints": ["不要读取 mykey.py", "不要提交"]},
    )
    runtime = _run_script(draft.script, run_id="wf_forward_workflow") if draft.validation.get("ok") else {}
    waves = runtime.get("jobWaves") or []
    return {
        "name": "workflow",
        "validationOk": bool(draft.validation.get("ok")),
        "mode": draft.plan.get("mode"),
        "phases": [phase.get("title") for phase in draft.plan.get("phases") or []],
        "runtime": runtime,
        "waveOrdered": waves == sorted(wave for wave in waves if isinstance(wave, int)),
        "expected": "planned-phases",
    }


def _case_delegated() -> dict:
    from workflow_planner import _normalize_workflow_execution_contract, render_workflow_plan

    plan = {
        "taskType": "research",
        "mode": "delegated",
        "riskLevel": "high",
        "phases": [
            {
                "title": "Collect",
                "agents": [
                    {"label": "a", "prompt": "collect a", "dependsOn": []},
                    {"label": "b", "prompt": "collect b", "dependsOn": []},
                ],
            },
            {"title": "Synthesis", "agents": [{"label": "s", "prompt": "synthesize", "dependsOn": ["a", "b"]}]},
        ],
    }
    normalized = _normalize_workflow_execution_contract(plan)
    script = render_workflow_plan(normalized)
    runtime = _run_script(script, run_id="wf_forward_delegated")
    return {
        "name": "delegated",
        "maxAgents": normalized["orchestration"]["maxAgents"],
        "maxWaves": normalized["orchestration"]["maxWaves"],
        "approvalRequired": normalized["orchestration"]["approvalRequired"],
        "runtime": runtime,
        "expected": "bounded-sidecars",
    }


def _case_fallback() -> dict:
    planner = LLMWorkflowPlanner(client=FailingPlannerClient(), max_repair_attempts=0)
    draft = planner.plan("做一个 review 计划")
    return {
        "name": "fallback",
        "plannerMode": draft.context.get("plannerMode"),
        "fallbackReasonPresent": bool(draft.context.get("fallbackReason")),
        "validationOk": bool(draft.validation.get("ok")),
        "expected": "fallback-reason-persisted",
    }


def _case_approval() -> dict:
    root = Path(tempfile.mkdtemp(prefix="ga_forward_approval_"))
    store = WorkflowStore(root)
    controller = WorkflowController(store)
    draft = WorkflowPlanner().plan("调研一个技术方案")
    draft.plan.setdefault("orchestration", {})["approvalRequired"] = True

    class FixedPlanner:
        def plan(self, _task_text, _context=None):
            return draft

    run = controller.create_planned_run(
        session_id="forward_approval",
        task_text="调研一个技术方案",
        planner=FixedPlanner(),
        auto_approve=True,
    )
    return {
        "name": "approval",
        "status": run.status,
        "gateReason": (run.metadata.get("approvalGate") or {}).get("reason"),
        "expected": "awaiting-approval",
    }


def _case_eval_contract(planner_client) -> dict:
    planner = LLMWorkflowPlanner(client=planner_client, max_repair_attempts=1)
    draft = planner.plan(
        "实现 workflow scheduler 的最小 TDD 切片：先理解，再写 failing tests，再实现，再验证。",
        context={"constraints": ["不要读取 mykey.py", "不要提交"]},
    )
    acceptance = draft.plan.get("acceptance") or {}
    checks = [str(c.get("type") if isinstance(c, dict) else c) for c in acceptance.get("checks") or []]
    schema_refs = {
        str(agent.get("schemaRef"))
        for phase in draft.plan.get("phases") or []
        for agent in phase.get("agents") or []
        if agent.get("schemaRef")
    }
    return {
        "name": "eval_contract",
        "validationOk": bool(draft.validation.get("ok")),
        "taskType": draft.plan.get("taskType"),
        "acceptanceChecks": checks,
        "hasVerificationSchema": "verification_schema" in checks,
        "strictSchemaRefs": sorted(schema_refs),
        "expected": "strict-evidence",
    }


def _live_model_smoke() -> dict:
    from workflow_llm import binding_from_profile, make_session

    binding = binding_from_profile(PROFILE)
    session = make_session(binding)
    answer = "".join(
        str(chunk)
        for chunk in session.ask({"role": "user", "content": [{"type": "text", "text": "Reply with exactly: MATRIX_OK"}]})
    )
    return {"profile": binding.profile_name, "model": binding.model_id, "answered": "MATRIX_OK" in answer}


def main() -> int:
    summary: dict = {"profile": PROFILE, "skipped": False, "issues": [], "cases": []}
    if not OPT_IN:
        summary.update({"skipped": True, "reason": "set GA_RUN_REAL_FORWARD_MATRIX=1 to run"})
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    start = time.time()
    try:
        summary["liveModelSmoke"] = _live_model_smoke()
        planner_client = RealPlannerClient(PROFILE)
        for builder in (_case_direct, _case_workflow):
            summary["cases"].append(builder(planner_client))
        summary["cases"].append(_case_delegated())
        summary["cases"].append(_case_fallback())
        summary["cases"].append(_case_approval())
        summary["cases"].append(_case_eval_contract(planner_client))
    except Exception as exc:
        summary["issues"].append(f"{type(exc).__name__}: {exc}")
    summary["durationSeconds"] = round(time.time() - start, 2)
    summary["matrixNames"] = [case["name"] for case in build_forward_test_matrix()]
    summary["passed"] = not summary["issues"]
    print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
    return 0 if summary["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
