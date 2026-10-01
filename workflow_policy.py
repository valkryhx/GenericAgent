"""Deterministic policy helpers for workflow routing and bounded delegation."""

from __future__ import annotations

from typing import Any


WORKFLOW_MODES = frozenset({"direct", "workflow", "delegated"})

# Bounded-sidecar budgets. These describe `delegated` mode specifically: the
# whole point of that mode is a small, fixed set of sidecars beside the parent.
MAX_DELEGATED_AGENTS = 5
MAX_DELEGATED_WAVES = 4

# Safety valves for ordinary workflows. These are NOT design limits. Complex
# tasks legitimately need many packets and deep dependency chains -- a 5-stage
# TDD chain (understand -> tests -> implement -> verify -> summary) is normal
# work, not runaway fan-out. Applying the delegated 5/4 budgets here silently
# killed such plans at registration time, so ordinary workflows are bounded by
# a runaway guard instead: the same shape Step-Code uses (DEFAULT_MAX_AGENTS =
# 1000) rather than a number that encodes an opinion about task size.
HARD_MAX_WORKFLOW_AGENTS = 1000
HARD_MAX_WORKFLOW_WAVES = 64


def plan_required_waves(plan: dict[str, Any] | None) -> int:
    """Longest dependency chain in the plan, counted in waves.

    The host needs this to size ``maxWaves`` correctly. The model's declared
    value is unreliable: a real coding plan declared ``maxWaves: 4`` while the
    plan itself contained a 5-deep chain, and the run died at wave 5 with
    ``delegation_max_waves_exceeded``. The graph is the fact; the declaration is
    a hint, so the host takes the larger of the two.
    """

    source = plan if isinstance(plan, dict) else {}
    dependencies: dict[str, list[str]] = {}
    for phase in source.get("phases") or []:
        if not isinstance(phase, dict):
            continue
        for agent in phase.get("agents") or []:
            if not isinstance(agent, dict):
                continue
            label = str(agent.get("label") or "")
            if not label:
                continue
            dependencies[label] = [str(item) for item in agent.get("dependsOn") or [] if str(item)]
    depth: dict[str, int] = {}

    def resolve(label: str, seen: frozenset[str]) -> int:
        if label in depth:
            return depth[label]
        if label in seen:
            return 1
        best = 1
        for dependency in dependencies.get(label, []):
            if dependency in dependencies:
                best = max(best, resolve(dependency, seen | {label}) + 1)
        depth[label] = best
        return best

    return max((resolve(label, frozenset()) for label in dependencies), default=0)


def route_workflow_mode(
    *,
    task_type: str | None,
    requested_mode: str | None,
    phase_count: int,
    risk_level: str | None,
) -> str:
    requested = str(requested_mode or "").strip().lower()
    task = str(task_type or "").strip().lower()
    risk = str(risk_level or "").strip().lower()
    phases = int(phase_count or 0)
    if requested == "delegated":
        return "delegated"
    # Small, phase-less, low-risk work should not be wrapped in a workflow even
    # when the model declares one. This keeps the direct/workflow boundary
    # deterministic instead of prompt-dependent.
    if phases <= 0 and risk in {"", "low"}:
        return "direct"
    if phases <= 1 and risk in {"", "low"} and task in {"planning", "direct"}:
        return "direct"
    if requested in WORKFLOW_MODES:
        return requested
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
    agent_count = len(
        [agent for phase in source.get("phases") or [] for agent in phase.get("agents") or []]
    )
    try:
        declared_agents = int(orchestration.get("maxAgents") or 0)
    except (TypeError, ValueError):
        declared_agents = 0
    try:
        declared_waves = int(orchestration.get("maxWaves") or 0)
    except (TypeError, ValueError):
        declared_waves = 0
    # The host never sizes the budget below what the plan actually contains.
    raw_agents = max(declared_agents, agent_count)
    raw_waves = max(declared_waves, phase_count, plan_required_waves(source))
    if mode == "delegated":
        max_agents = max(1, min(MAX_DELEGATED_AGENTS, raw_agents))
        max_waves = max(1, min(MAX_DELEGATED_WAVES, raw_waves))
    else:
        max_agents = max(1, min(HARD_MAX_WORKFLOW_AGENTS, raw_agents))
        max_waves = max(1, min(HARD_MAX_WORKFLOW_WAVES, raw_waves))
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
