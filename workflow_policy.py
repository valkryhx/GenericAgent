"""Deterministic policy helpers for workflow routing and bounded delegation."""

from __future__ import annotations

from typing import Any


WORKFLOW_MODES = frozenset({"direct", "workflow", "delegated"})
MAX_DELEGATED_AGENTS = 5
MAX_DELEGATED_WAVES = 4


def route_workflow_mode(
    *,
    task_type: str | None,
    requested_mode: str | None,
    phase_count: int,
    risk_level: str | None,
) -> str:
    requested = str(requested_mode or "").strip().lower()
    if requested in WORKFLOW_MODES:
        return requested
    task = str(task_type or "").strip().lower()
    risk = str(risk_level or "").strip().lower()
    if int(phase_count or 0) <= 0 and task in {"planning", "review", "direct"} and risk in {"", "low"}:
        return "direct"
    return "workflow"


def normalize_delegation_policy(plan: dict[str, Any] | None) -> dict[str, Any]:
    source = plan if isinstance(plan, dict) else {}
    orchestration = source.get("orchestration") if isinstance(source.get("orchestration"), dict) else {}
    phase_count = len(source.get("phases") or [])
    mode = route_workflow_mode(
        task_type=source.get("taskType"),
        requested_mode=source.get("mode"),
        phase_count=phase_count,
        risk_level=source.get("riskLevel"),
    )
    raw_agents = orchestration.get("maxAgents") or len(
        [agent for phase in source.get("phases") or [] for agent in phase.get("agents") or []]
    )
    raw_waves = orchestration.get("maxWaves") or phase_count or 1
    max_agents = max(1, min(MAX_DELEGATED_AGENTS, int(raw_agents)))
    max_waves = max(1, min(MAX_DELEGATED_WAVES, int(raw_waves)))
    delegation_allowed = bool(orchestration.get("delegationAllowed", mode == "delegated"))
    approval_required = bool(orchestration.get("approvalRequired", False)) or mode == "delegated"
    failure_policy = str(orchestration.get("failurePolicy") or "continue")
    if failure_policy not in {"continue", "fail_fast"}:
        failure_policy = "continue"
    return {
        "mode": mode,
        "maxAgents": max_agents,
        "maxWaves": max_waves,
        "delegationAllowed": delegation_allowed,
        "approvalRequired": approval_required,
        "failurePolicy": failure_policy,
    }


def build_forward_test_matrix() -> list[dict[str, str]]:
    return [
        {"name": "direct", "mode": "direct", "expected": "single-turn"},
        {"name": "workflow", "mode": "workflow", "expected": "planned-phases"},
        {"name": "delegated", "mode": "delegated", "expected": "bounded-sidecars"},
        {"name": "fallback", "mode": "workflow", "expected": "fallback-reason-persisted"},
        {"name": "approval", "mode": "delegated", "expected": "awaiting-approval"},
        {"name": "eval_contract", "mode": "workflow", "expected": "strict-evidence"},
    ]
