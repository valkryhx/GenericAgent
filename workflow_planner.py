from __future__ import annotations

import copy
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any

from workflow_policy import normalize_delegation_policy
from workflow_activation import resolve_workflow_activation
from workflow_verification import normalize_verification_contract
from workflow_tool_profiles import (
    CAPABILITY_CLASSES,
    format_capability_classes,
    format_role_profile_guidance,
    format_tool_profile_guidance,
    is_known_tool_profile,
)


CODING_AGENT_ROLES = frozenset(
    {
        "understanding",
        "research",
        "contract",
        "tests",
        "implementation",
        "verification",
        "review",
        "repair",
        "summary",
        "synthesis",
    }
)

GA_WORKFLOW_VERIFICATION_SCHEMA = {
    "type": "object",
    "required": ["verificationPassed", "checks", "blockingIssues"],
    "properties": {
        "verificationPassed": {"type": "boolean"},
        "checks": {"type": "array"},
        "blockingIssues": {"type": "array"},
    },
}

WORKFLOW_MODES = frozenset({"direct", "workflow", "delegated"})
WORKFLOW_RISK_LEVELS = frozenset({"low", "medium", "high"})
ARTIFACT_ACCEPTANCE_CHECKS = frozenset({
    "artifact_exists",
    "artifact_readback",
    "artifact_structure",
    "required_tool_evidence",
    "source_count",
    "schema_valid",
    "command_exit_zero",
    "no_secret_pattern",
})


def _normalize_plan_retry_policy(value: Any) -> dict[str, Any]:
    raw = value if isinstance(value, dict) else {}
    try:
        max_attempts = int(raw.get("maxAttempts", 2))
    except (TypeError, ValueError):
        max_attempts = 2
    try:
        backoff_ms = int(raw.get("backoffMs", 0))
    except (TypeError, ValueError):
        backoff_ms = 0
    retryable = raw.get("retryableErrors")
    if not isinstance(retryable, list) or not retryable:
        retryable = ["timeout", "transient", "rate_limit", "provider_anomaly", "mcp_transient", "schema_validation_failed"]
    return {
        "maxAttempts": max(1, min(3, max_attempts)),
        "retryableErrors": [str(item) for item in retryable if str(item).strip()],
        "backoffMs": max(0, min(30_000, backoff_ms)),
    }


CODE_PRODUCING_ROLES = frozenset({"implementation", "tests", "repair"})


def normalize_plan_workspace_paths(plan: dict[str, Any], workspace_root) -> dict[str, Any]:
    """Canonicalize declared artifact paths before validation and scheduling."""
    from workflow_workspace import normalize_declared_artifact_path

    normalized = copy.deepcopy(plan if isinstance(plan, dict) else {})
    contract = normalized.get("executionContract")
    artifacts = contract.get("artifacts") if isinstance(contract, dict) else []
    replacements: dict[str, str] = {}
    for artifact in artifacts or []:
        if not isinstance(artifact, dict) or not str(artifact.get("path") or "").strip():
            continue
        raw = str(artifact["path"])
        relative = normalize_declared_artifact_path(raw, workspace_root)
        artifact["path"] = relative
        replacements[raw] = relative
        replacements["/" + relative] = relative
        if relative.startswith("tmp/"):
            replacements["/tmp/" + relative[4:]] = relative

    for phase in normalized.get("phases") or []:
        if not isinstance(phase, dict):
            continue
        for agent in phase.get("agents") or []:
            if not isinstance(agent, dict):
                continue
            for field in ("writeScope", "deliverables"):
                values = agent.get(field)
                if not isinstance(values, list):
                    continue
                updated = []
                for value in values:
                    raw = str(value)
                    try:
                        relative = normalize_declared_artifact_path(raw, workspace_root)
                    except ValueError:
                        relative = raw
                    replacements[raw] = relative
                    updated.append(relative)
                agent[field] = updated
            prompt = agent.get("prompt")
            if isinstance(prompt, str):
                for raw, relative in replacements.items():
                    prompt = prompt.replace(raw, relative)
                agent["prompt"] = prompt

    def rewrite(value):
        if isinstance(value, str):
            for raw, relative in replacements.items():
                value = value.replace(raw, relative)
            return value
        if isinstance(value, list):
            return [rewrite(item) for item in value]
        if isinstance(value, dict):
            return {key: rewrite(item) for key, item in value.items()}
        return value

    return rewrite(normalized)


def plan_produces_code(plan: dict[str, Any] | None) -> bool:
    """Whether a plan's own agents declare code-producing work.

    This is a contract-consistency signal, not a prediction about the task.

    GA once treated ``writeScope`` as proof of a coding task, so a five-agent
    Tavily research plan that wrote JSON evidence and an HTML report was held to
    the coding contract and rejected with ``missing_verification_check``. Writing
    a file is not writing code: research, review and report plans all persist
    artifacts. Only the declared code-producing roles activate the coding rules,
    and ``taskType`` stays a planner hint.

    Role is optional metadata. A plan that declares no code role is simply not
    held to coding topology; the enforceable contract is the checks it declares.
    """

    source = plan if isinstance(plan, dict) else {}
    for phase in source.get("phases") or []:
        if not isinstance(phase, dict):
            continue
        for agent in phase.get("agents") or []:
            if not isinstance(agent, dict):
                continue
            if str(agent.get("role") or "").strip().lower() in CODE_PRODUCING_ROLES:
                return True
    return False


def plan_declares_writes(plan: dict[str, Any] | None) -> bool:
    """Whether a plan promises to persist any surface at all.

    Used only to decide whether the plan owes the host at least one evaluable
    required check. It deliberately says nothing about task type: a read-only
    research plan owes nothing, while a research plan that writes evidence files
    and a report owes the same artifact checks a coding plan owes its sources.
    """

    source = plan if isinstance(plan, dict) else {}
    contract = source.get("executionContract")
    if isinstance(contract, dict) and contract.get("artifacts"):
        return True
    for phase in source.get("phases") or []:
        if not isinstance(phase, dict):
            continue
        for agent in phase.get("agents") or []:
            if not isinstance(agent, dict):
                continue
            if agent.get("writeScope") or agent.get("deliverables"):
                return True
    return False


def plan_declares_tests(plan: dict[str, Any] | None) -> bool:
    """Whether the plan itself says it will produce Python tests.

    Used by the runtime to decide whether an empty unittest gate means "this
    workflow never promised tests" (not applicable) or "the promised tests are
    missing" (hard failure). Nothing else should infer test intent.
    """

    source = plan if isinstance(plan, dict) else {}
    for phase in source.get("phases") or []:
        if not isinstance(phase, dict):
            continue
        for agent in phase.get("agents") or []:
            if not isinstance(agent, dict):
                continue
            if str(agent.get("role") or "").strip().lower() == "tests":
                return True
    return False


def plan_shared_surfaces(plan: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Derive producer→consumer handoff surfaces from the plan's own graph.

    Models do not reliably declare ``sharedSurfaces`` (four real runs produced
    ``[]`` every time), so the host computes the surface list from ``dependsOn``
    instead of trusting a self-report. A surface exists wherever one agent's
    output is consumed by another agent; ``structured`` records whether the
    producer promised a machine-checkable schema or only free text.
    """

    source = plan if isinstance(plan, dict) else {}
    schemas = source.get("schemas") if isinstance(source.get("schemas"), dict) else {}
    producer_schema: dict[str, str] = {}
    for phase in source.get("phases") or []:
        if not isinstance(phase, dict):
            continue
        for agent in phase.get("agents") or []:
            if not isinstance(agent, dict):
                continue
            label = str(agent.get("label") or "")
            schema_ref = str(agent.get("schemaRef") or "")
            if label and schema_ref:
                producer_schema[label] = schema_ref

    consumers: dict[str, list[str]] = {}
    order: list[str] = []
    for phase in source.get("phases") or []:
        if not isinstance(phase, dict):
            continue
        for agent in phase.get("agents") or []:
            if not isinstance(agent, dict):
                continue
            consumer = str(agent.get("label") or "")
            for dependency in agent.get("dependsOn") or []:
                producer = str(dependency)
                if not producer:
                    continue
                if producer not in consumers:
                    consumers[producer] = []
                    order.append(producer)
                if consumer and consumer not in consumers[producer]:
                    consumers[producer].append(consumer)

    surfaces: list[dict[str, Any]] = []
    for producer in order:
        schema_ref = producer_schema.get(producer) or ""
        surfaces.append(
            {
                "surface": schema_ref or f"{producer}->handoff",
                "producer": producer,
                "consumers": list(consumers[producer]),
                "structured": bool(schema_ref and isinstance(schemas.get(schema_ref), dict)),
            }
        )
    return surfaces


def plan_risk_level(plan: dict[str, Any] | None) -> str:
    """Compute risk from the plan's shape, not from a label the model guessed.

    ``riskLevel`` used to default from ``taskType``, which quietly reintroduced
    the very coupling the eval-contract axis removed: identical phases scored
    ``high`` under a ``coding`` label and ``low`` under a ``research`` label,
    and ``high`` then forced ``full``. Risk is now derived from what the plan
    actually declares, with a model-declared level only able to escalate: a
    model may raise it, never lower it.
    """

    source = plan if isinstance(plan, dict) else {}
    phases = [phase for phase in (source.get("phases") or []) if isinstance(phase, dict)]
    write_capable = [
        agent
        for phase in phases
        for agent in phase.get("agents") or []
        if isinstance(agent, dict)
        and (
            str(agent.get("role") or "").strip().lower() in CODE_PRODUCING_ROLES
            or bool(agent.get("writeScope"))
        )
    ]
    surfaces = plan_shared_surfaces(source)
    structured = any(surface["structured"] for surface in surfaces)

    if structured or len(write_capable) > 1 or (surfaces and write_capable):
        computed = "high"
    elif write_capable or surfaces:
        computed = "medium"
    else:
        computed = "low"

    declared = str(source.get("riskLevel") or "").strip().lower()
    order = {"low": 0, "medium": 1, "high": 2}
    if declared in order and order[declared] > order[computed]:
        return declared
    return computed


def plan_eval_level(plan: dict[str, Any] | None) -> str:
    """Pick the eval-contract level from the plan's declared shape.

    ``taskType`` is deliberately ignored: the same real task produced ``inline``
    on one run and ``full`` on another when the level was derived from that
    label. The level now answers "how much integration risk does this plan
    carry", computed from phases, handoff surfaces, and code writers.
    """

    source = plan if isinstance(plan, dict) else {}
    phases = [phase for phase in (source.get("phases") or []) if isinstance(phase, dict)]
    if not phases:
        return "none"

    # ultracode's criterion is explicit: "one packet produces a surface another
    # packet consumes" is already full, because the downstream packet can no
    # longer be verified in isolation. The host does not second-guess that with
    # its own fragility heuristic -- under-constrained handoffs are exactly the
    # failure mode this axis exists to remove, and the extra cost of `full` is
    # one evidence file, not a new blocking gate.
    if plan_shared_surfaces(source):
        return "full"
    write_capable = {
        str(agent.get("label") or "")
        for phase in phases
        for agent in phase.get("agents") or []
        if isinstance(agent, dict)
        and (
            str(agent.get("role") or "").strip().lower() in CODE_PRODUCING_ROLES
            or bool(agent.get("writeScope"))
        )
    }
    if str(source.get("riskLevel") or "").strip().lower() == "high":
        return "full"
    if len(write_capable) > 1:
        return "full"
    return "inline"


def _plan_declares_code_work(plan: dict[str, Any], task_type: str = "") -> bool:
    """Backward-compatible alias for :func:`plan_produces_code`."""

    return plan_produces_code(plan)


def _normalize_workflow_execution_contract(plan: dict[str, Any]) -> dict[str, Any]:
    """Add a stable, inspectable orchestration contract to model-produced plans."""

    normalized = copy.deepcopy(plan)
    phases = normalized.get("phases") or []
    # Models may follow the object-shaped action example in the execution
    # contract and emit [{"id": "search", "tool": "..."}] while the
    # validator/runtime consume the compact packet form ["search"].
    # Normalize both representations at the boundary, preserving unknown ids
    # so strict validation can still reject genuinely unmapped actions.
    for phase in phases:
        for agent in phase.get("agents") or []:
            if not isinstance(agent, dict) or not isinstance(agent.get("actions"), list):
                continue
            action_ids: list[str] = []
            for action in agent["actions"]:
                action_id = (
                    str(action.get("id") or "").strip()
                    if isinstance(action, dict)
                    else str(action or "").strip()
                )
                if action_id and action_id not in action_ids:
                    action_ids.append(action_id)
            agent["actions"] = action_ids
    task_type = str(normalized.get("taskType") or "planning").strip().lower()
    mode = str(normalized.get("mode") or "").strip().lower()
    if mode not in WORKFLOW_MODES:
        mode = "workflow" if phases else "direct"
    # Host-computed, shape-derived; a declared value can only escalate it.
    risk_level = plan_risk_level(normalized)
    if risk_level not in WORKFLOW_RISK_LEVELS:
        risk_level = "medium"

    acceptance = normalized.get("acceptance") if isinstance(normalized.get("acceptance"), dict) else {}
    # A host-side python unittest gate is only satisfiable when the plan actually
    # produces code. Models frequently copy the coding acceptance example into
    # research/review/planning plans, which then run an empty test gate and fail
    # with "NO TESTS RAN". Decide from what the plan declares (a code-producing
    # role), not from the taskType label, so mixed plans that really do write
    # code keep their gate.
    if acceptance.get("checks") and not plan_produces_code(normalized):
        acceptance = copy.deepcopy(acceptance)
        acceptance["checks"] = [
            check
            for check in acceptance.get("checks") or []
            if str(check.get("type") if isinstance(check, dict) else check).strip() != "python_unittest"
        ]
        normalized["acceptance"] = acceptance
    acceptance_checks = [
        str(check.get("type") if isinstance(check, dict) else check).strip()
        for check in acceptance.get("checks") or []
        if str(check.get("type") if isinstance(check, dict) else check).strip()
    ]
    execution_contract = normalized.get("executionContract")
    if isinstance(execution_contract, dict):
        execution_contract = copy.deepcopy(execution_contract)
        normalized_evidence = []
        for raw_evidence in execution_contract.get("requiredToolEvidence") or []:
            if not isinstance(raw_evidence, dict):
                normalized_evidence.append(raw_evidence)
                continue
            evidence = copy.deepcopy(raw_evidence)
            tool = str(evidence.get("tool") or "").strip()
            mode = str(evidence.get("mode") or "").strip().lower()
            if not mode:
                mode = "preferred" if tool in {"file_write", "file_patch"} else "required"
            evidence["mode"] = mode
            normalized_evidence.append(evidence)
        execution_contract["requiredToolEvidence"] = normalized_evidence
        normalized_capability_evidence = []
        for raw_capability in execution_contract.get("requiredCapabilityEvidence") or []:
            if not isinstance(raw_capability, dict):
                normalized_capability_evidence.append(raw_capability)
                continue
            entry = copy.deepcopy(raw_capability)
            capability = str(entry.get("capability") or "").strip()
            entry_mode = str(entry.get("mode") or "").strip().lower()
            if not entry_mode:
                entry_mode = "preferred" if capability in {"file_write", "file_read"} else "required"
            entry["mode"] = entry_mode
            normalized_capability_evidence.append(entry)
        if normalized_capability_evidence:
            execution_contract["requiredCapabilityEvidence"] = normalized_capability_evidence
        for artifact in execution_contract.get("artifacts") or []:
            if not isinstance(artifact, dict):
                continue
            if not artifact.get("writeModes"):
                artifact["writeModes"] = ["file_write", "file_patch", "code_run"]
        normalized["executionContract"] = execution_contract
    evidence_contract = execution_contract.get("requiredToolEvidence") or [] if isinstance(execution_contract, dict) else []
    verification = normalized.get("verification")
    if evidence_contract and isinstance(verification, dict) and isinstance(verification.get("checks"), list):
        verification = copy.deepcopy(verification)
        verification["checks"] = [
            check for check in verification["checks"]
            if not (
                isinstance(check, dict)
                and str(check.get("id") or "").strip() == "required_tool_evidence"
            )
        ]

    # --- Artifact verification ownership ---------------------------------
    # ``executionContract.artifacts`` is the single source of truth for an
    # artifact's path, writer and existence/readback checks. ``verification``
    # checks of kind ``artifact`` are a host-derived view of that contract, not
    # an independent registry the model must keep in sync (two registries drift
    # exactly the way real runs drifted). Binding is deterministic and never
    # guessed from prose, owner labels or file extensions:
    #   1. an explicit ``path`` (``artifact``/``artifactRef`` are accepted
    #      aliases and normalized to ``path``);
    #   2. a check id that names one artifact's declared required check and is
    #      declared by exactly one artifact;
    #   3. exactly one declared artifact in the whole contract.
    # A pathless check that cannot be bound is dropped: the execution contract
    # already enforces it, so keeping an unbound required check only creates a
    # spurious blocking failure.
    if isinstance(verification, dict) and isinstance(verification.get("checks"), list):
        verification = copy.deepcopy(verification)
        artifact_check_ids = {"artifact_exists", "artifact_readback", "artifact_structure", "source_count"}
        check_paths: dict[str, set[str]] = {}
        artifact_paths: set[str] = set()
        if isinstance(execution_contract, dict):
            for artifact in execution_contract.get("artifacts") or []:
                if not isinstance(artifact, dict):
                    continue
                path = str(artifact.get("path") or "").replace("\\", "/").strip("/")
                if not path:
                    continue
                artifact_paths.add(path)
                for check_id in artifact.get("requiredChecks") or []:
                    check_paths.setdefault(str(check_id), set()).add(path)

        kept_checks = []
        for check in verification["checks"]:
            if not isinstance(check, dict):
                kept_checks.append(check)
                continue
            check_id = str(check.get("id") or "").strip()
            if check_id in artifact_check_ids:
                check["kind"] = "artifact"
            if str(check.get("kind") or "").strip().lower() == "artifact":
                path = str(check.get("path") or "").strip()
                for alias in ("artifact", "artifactRef"):
                    if not path and isinstance(check.get(alias), str) and check[alias].strip():
                        path = check[alias].strip()
                if not path:
                    candidates = check_paths.get(check_id, set())
                    if len(candidates) == 1:
                        path = next(iter(candidates))
                if not path and len(artifact_paths) == 1:
                    path = next(iter(artifact_paths))
                if path:
                    check["path"] = path.replace("\\", "/").strip("/")
                else:
                    # Defer to the authoritative execution contract instead of
                    # keeping an unbound required artifact check that can only
                    # fail the run for the wrong reason.
                    continue
            elif str(check.get("kind") or "").strip().lower() == "schema":
                # GA has no host evaluator wired from a plan schema to a
                # standalone verification check, and no child is required to
                # emit evidence for an arbitrary model-named schema id. Treating
                # such a restatement as a hard gate fails runs that produced a
                # valid artifact. Keep it visible but advisory; the enforceable
                # requirement is the artifact's own ``schema_valid`` check on the
                # execution contract.
                check["required"] = False
                check["advisory"] = True
                check["deferredToExecutionContract"] = True
            kept_checks.append(check)
        # If the plan declares no required check, materialize the host-owned
        # ones from the authoritative execution contract. ``requiredChecks`` on
        # the artifact contract are host-defined machine checks
        # (artifact_exists / artifact_readback / ...), so the derived entries are
        # always bindable and never guessed. An advisory-only verification view
        # (the shape real research plans produce) must not leave the run with
        # nothing evaluable.
        has_required_check = any(
            isinstance(check, dict) and check.get("required") is True for check in kept_checks
        )
        if not has_required_check and isinstance(execution_contract, dict):
            derived: list[dict[str, Any]] = []
            seen_derived: set[tuple[str, str]] = set()
            for artifact in execution_contract.get("artifacts") or []:
                if not isinstance(artifact, dict):
                    continue
                path = str(artifact.get("path") or "").replace("\\", "/").strip("/")
                if not path:
                    continue
                for check_id in artifact.get("requiredChecks") or []:
                    check_id = str(check_id)
                    key = (check_id, path)
                    if not check_id or key in seen_derived:
                        continue
                    seen_derived.add(key)
                    derived.append({
                        "id": f"{Path(path).stem}_{check_id}",
                        "kind": "artifact",
                        "check": check_id,
                        "path": path,
                        "required": True,
                        "owner": "host",
                        "derivedFromExecutionContract": True,
                    })
            # Append, never replace: a model-authored advisory/enumerated check
            # (for example a schema restatement downgraded to advisory) is still
            # part of the audit view, and the host-owned derived checks are what
            # make the contract evaluable.
            verification["checks"] = [*kept_checks, *derived] if derived else kept_checks
        else:
            verification["checks"] = kept_checks
        normalized["verification"] = verification
    verification_contract = normalize_verification_contract({**normalized, "acceptance": acceptance})
    normalized["verification"] = copy.deepcopy(verification_contract)
    success_criteria = normalized.get("successCriteria")
    if not isinstance(success_criteria, list) or not success_criteria:
        success_criteria = [f"acceptance check passes: {check}" for check in acceptance_checks]
    if not success_criteria:
        success_criteria = ["all declared workflow phases complete", "workflow artifacts are persisted"]

    eval_contract = normalized.get("evalContract") if isinstance(normalized.get("evalContract"), dict) else {}
    required_checks = list(eval_contract.get("requiredChecks") or [])
    for check in ["plan_validation", *acceptance_checks]:
        if check not in required_checks:
            required_checks.append(check)
    # Both the level and the surface list are host-computed. Model-supplied
    # values were unreliable in real runs (sharedSurfaces was [] every time and
    # the level flipped with the taskType label), so the plan's own graph wins.
    computed_surfaces = plan_shared_surfaces({**normalized, "evalContract": eval_contract})
    eval_contract = {
        "level": plan_eval_level({**normalized, "evalContract": eval_contract}),
        "outcome": str(eval_contract.get("outcome") or "workflow completes with evidence-backed acceptance result"),
        "sharedSurfaces": computed_surfaces,
        "requiredChecks": required_checks,
        "blockingConditions": list(eval_contract.get("blockingConditions") or ["plan_validation_failed", "acceptance_failed"]),
        "handoffEvidence": list(eval_contract.get("handoffEvidence") or ["summary", "evidence", "blockingIssues"]),
    }

    labels = [
        str(agent.get("label") or "")
        for phase in phases
        for agent in phase.get("agents") or []
        if str(agent.get("label") or "")
    ]
    orchestration = normalized.get("orchestration") if isinstance(normalized.get("orchestration"), dict) else {}
    orchestration = {
        "maxAgents": int(orchestration.get("maxAgents") or len(labels)),
        "maxWaves": int(orchestration.get("maxWaves") or len(phases)),
        "delegationAllowed": bool(orchestration.get("delegationAllowed", mode == "delegated")),
        "failurePolicy": str(orchestration.get("failurePolicy") or ("fail_fast" if acceptance.get("failWorkflowOnError") else "continue")),
        "parentCriticalPath": list(orchestration.get("parentCriticalPath") or labels[-1:]),
        "waitPoints": list(orchestration.get("waitPoints") or [str(phase.get("title") or "") for phase in phases]),
    }
    policy = normalize_delegation_policy({**normalized, "mode": mode, "riskLevel": risk_level, "orchestration": orchestration})
    orchestration.update(policy)
    mode = policy["mode"]

    normalized["workflowContractVersion"] = int(normalized.get("workflowContractVersion") or 1)
    normalized["mode"] = mode
    normalized["riskLevel"] = risk_level
    normalized["successCriteria"] = [str(item) for item in success_criteria]
    normalized["verification"] = copy.deepcopy(verification_contract)
    normalized["evalContract"] = eval_contract
    normalized["orchestration"] = orchestration
    for phase in phases:
        for agent in phase.get("agents") or []:
            agent.setdefault("owner", "workflow")
            agent.setdefault("writeScope", [])
            agent.setdefault("deliverables", [])
            agent["retryPolicy"] = _normalize_plan_retry_policy(agent.get("retryPolicy"))
    return normalized


def _normalize_execution_contract_assignments(plan: dict[str, Any]) -> dict[str, Any]:
    """Copy explicit action/tool/artifact ownership into executable packets."""
    normalized = copy.deepcopy(plan)
    contract = normalized.get("executionContract")
    if not isinstance(contract, dict) or contract.get("requiresExecution") is not True:
        return normalized

    agents_by_label = {
        str(agent.get("label") or ""): agent
        for phase in normalized.get("phases") or []
        for agent in phase.get("agents") or []
        if isinstance(agent, dict) and str(agent.get("label") or "")
    }

    def add(agent: dict[str, Any], field: str, value: str) -> None:
        values = agent.get(field)
        if not isinstance(values, list):
            values = []
        if value not in [str(item) for item in values]:
            values.append(value)
        agent[field] = values

    for item in contract.get("actions") or []:
        if isinstance(item, dict):
            action_id = str(item.get("id") or "").strip()
            agent = agents_by_label.get(str(item.get("agent") or "").strip())
            if action_id and agent:
                add(agent, "actions", action_id)

    required_tools = {str(item).strip() for item in contract.get("requiredTools") or [] if str(item).strip()}
    for item in contract.get("requiredToolEvidence") or []:
        if isinstance(item, dict):
            tool = str(item.get("tool") or "").strip()
            agent = agents_by_label.get(str(item.get("agent") or "").strip())
            if tool in required_tools and agent:
                add(agent, "requiredTools", tool)

    for item in contract.get("requiredCapabilityEvidence") or []:
        if not isinstance(item, dict):
            continue
        capability = str(item.get("capability") or "").strip()
        agent = agents_by_label.get(str(item.get("agent") or "").strip())
        if capability and agent:
            add(agent, "capabilities", capability)

    for artifact in contract.get("artifacts") or []:
        if not isinstance(artifact, dict):
            continue
        path = str(artifact.get("path") or "").replace("\\", "/").strip("/")
        writer = agents_by_label.get(str(artifact.get("writer") or "").strip())
        if not path or not writer:
            continue
        add(writer, "writeScope", path)
        add(writer, "deliverables", path)
        # The host always enforces existence for a declared, non-optional
        # artifact. Materialize that check deterministically instead of asking
        # the model to remember it, so a correct plan is never rejected for
        # "missing artifact acceptance check" and the audit view matches what
        # the runtime actually enforces.
        if not artifact.get("optional") and not artifact.get("requiredChecks"):
            artifact["requiredChecks"] = ["artifact_exists"]
        checks = {str(item).strip() for item in artifact.get("requiredChecks") or [] if str(item).strip()}
        for check in checks:
            add(writer, "acceptanceChecks", check)
        # Only materialize local tools named by the contract; writer/path and
        # readback semantics provide their explicit ownership.
        if "file_write" in required_tools:
            add(writer, "requiredTools", "file_write")
        if "code_run" in required_tools:
            # A writable artifact may be produced by an allowlisted code
            # action (for example python-docx) instead of file_write.
            # Materialize the declared capability on the actual writer so
            # validation checks the union consistently.
            add(writer, "requiredTools", "code_run")
        if "artifact_readback" in checks and "file_read" in required_tools:
            add(writer, "requiredTools", "file_read")
    return normalized


def _normalize_plan_contract(plan: dict[str, Any]) -> dict[str, Any]:
    """Apply legacy conversion, topology normalization, and runtime contracts.

    Legacy acceptance fields are preserved as checks; no verification role or
    output schema is synthesized. Review plans and execution share the shape.
    """

    normalized = _normalize_coding_acceptance_contract(plan)
    normalized = _split_same_phase_dependencies(normalized)
    normalized = _normalize_execution_contract_assignments(normalized)
    return _normalize_workflow_execution_contract(normalized)


def _split_same_phase_dependencies(plan: dict[str, Any]) -> dict[str, Any]:
    """Split agents that depend on same-phase peers into explicit later phases.

    Models routinely pack a verifier and the synthesis consuming it into one
    phase. GA's validator rejects that topology, so normalize it
    deterministically instead of spending a repair round-trip or weakening the
    hard gate. Intra-phase dependencies become their own later phase; cross
    phase dependencies are left untouched.
    """

    normalized = copy.deepcopy(plan)
    phases = normalized.get("phases")
    if not isinstance(phases, list) or not phases:
        return normalized

    rebuilt: list[dict[str, Any]] = []
    for phase in phases:
        title = str(phase.get("title") or "")
        agents = [agent for agent in (phase.get("agents") or []) if isinstance(agent, dict)]
        pending = {str(agent.get("label") or ""): agent for agent in agents}
        levels: dict[str, int] = {}
        for _ in range(len(agents) + 1):
            progressed = False
            for label, agent in pending.items():
                if label in levels:
                    continue
                intra = [
                    str(item)
                    for item in (agent.get("dependsOn") or [])
                    if str(item) in pending
                ]
                if not intra or all(item in levels for item in intra):
                    levels[label] = 1 + max((levels[item] for item in intra), default=-1) if intra else 0
                    progressed = True
            if not progressed:
                break
        if len(levels) != len(pending):
            rebuilt.append({"title": title, "agents": agents})
            continue
        max_level = max(levels.values(), default=0)
        for level in range(max_level + 1):
            bucket = [
                agent
                for agent in agents
                if levels.get(str(agent.get("label") or "")) == level
            ]
            if not bucket:
                continue
            if level == 0:
                part_title = title
            else:
                part_title = f"{title} (Part {level + 1})"
            rebuilt.append({"title": part_title, "agents": bucket})
    normalized["phases"] = rebuilt
    return normalized


def _normalize_coding_acceptance_contract(plan: dict[str, Any]) -> dict[str, Any]:
    """Keep legacy name while avoiding implicit verifier/schema injection.

    Legacy acceptance fields are converted by
    :func:`_normalize_workflow_execution_contract`; plan topology and explicit
    verification checks remain model-declared and are validated separately.
    """

    return copy.deepcopy(plan)


@dataclass
class WorkflowDraft:
    task_text: str
    classification: dict[str, Any]
    plan: dict[str, Any]
    validation: dict[str, Any]
    script: str
    context: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "taskText": self.task_text,
            "classification": copy.deepcopy(self.classification),
            "plan": copy.deepcopy(self.plan),
            "validation": copy.deepcopy(self.validation),
            "script": self.script,
            "context": copy.deepcopy(self.context),
        }


class WorkflowPlanner:
    def plan(self, task_text: str, context: dict[str, Any] | None = None) -> WorkflowDraft:
        context = copy.deepcopy(context or {})
        classification = self.classify(task_text, context)
        plan = _normalize_workflow_execution_contract(self._build_plan(task_text, context, classification))
        validation = validate_workflow_plan(plan)
        script = render_workflow_plan(plan) if validation["ok"] else ""
        return WorkflowDraft(
            task_text=task_text,
            context=context,
            classification=classification,
            plan=plan,
            validation=validation,
            script=script,
        )

    def classify(self, task_text: str, context: dict[str, Any] | None = None) -> dict[str, Any]:
        text = str(task_text or "")
        lowered = text.lower()
        activation = resolve_workflow_activation(text)
        declared = context.get("activation") if isinstance(context, dict) else None
        declared_action = (
            str((declared or {}).get("action") or "").strip().lower()
            if isinstance(declared, dict)
            else ""
        )
        # The ink UI strips the "/workflow " prefix before planning, so the
        # planner cannot see the user's explicit opt-in in the text itself; the
        # caller carries it in context["activation"] (the auto-recommended path
        # already did). Without it, an execution task whose wording misses the
        # keyword lists below collapsed into the planner-only template and the
        # run reported success having written nothing but PLAN.md
        # (real case: wf_622266234f1345359d4e5f999758b922).
        explicit_execution = "requested" in {declared_action, str(activation.action).strip().lower()}
        if activation.plan_only:
            task_type = "planning"
            read_write_mode = "read_only"
            needs_code_change = False
        elif activation.action == "recommended":
            task_type = "mixed"
            read_write_mode = "may_write" if "artifact" in activation.matched_signals else "read_only"
            needs_code_change = "artifact" in activation.matched_signals
        elif any(word in text for word in ("\u8c03\u7814", "\u7814\u7a76", "\u8d44\u6599", "\u6765\u6e90")) or any(word in lowered for word in ("research", "source")):
            task_type = "research"
            read_write_mode = "read_only"
            needs_code_change = False
        elif any(word in text for word in ("\u5b9e\u73b0", "\u4fee\u590d", "\u5f00\u53d1", "\u4fee\u6539")) or any(word in lowered for word in ("implement", "fix", "code")):
            task_type = "coding"
            read_write_mode = "may_write"
            needs_code_change = True
        elif any(word in text for word in ("\u5ba1\u67e5", "\u8bc4\u5ba1", "review")) or "review" in lowered:
            task_type = "review"
            read_write_mode = "read_only"
            needs_code_change = False
        elif explicit_execution:
            # Explicit workflow opt-in with no recognisable shape: the task still
            # has to be executed. Guessing "coding" from the wording is exactly
            # the keyword coupling this planner is moving away from, so the type
            # stays advisory ("general") and the host resolves the real contract
            # from the declared capability classes.
            task_type = "general"
            read_write_mode = "may_write"
            needs_code_change = False
        else:
            task_type = "planning"
            read_write_mode = "read_only"
            needs_code_change = False
        return {
            "taskType": task_type,
            "readWriteMode": read_write_mode,
            "needsMcp": "search" in activation.matched_signals,
            "needsCodeChange": needs_code_change,
            "needsVerification": explicit_execution or activation.action != "none" or task_type in {"coding", "mixed"},
            "riskLevel": "medium" if needs_code_change else "low",
            "clarifyingQuestions": [],
            "constraints": list((context or {}).get("constraints") or []),
        }

    def _build_plan(self, task_text: str, context: dict[str, Any], classification: dict[str, Any]) -> dict[str, Any]:
        if classification["taskType"] == "mixed":
            html_output = bool(re.search(r"\bhtml\b|\bweb\s*page\b|\u7f51\u9875|html", task_text, re.I))
            artifact_path = "artifacts/overview.html" if html_output else "artifacts/summary.md"
            research_artifact_path = "artifacts/research-sources.json"
            artifact_kind = "HTML" if html_output else "Markdown"
            # No tool-name guessing: the plan declares a host-owned tool
            # profile plus capability classes, and the host resolves them
            # against the tools that are actually connected in this run.
            return {
                "taskType": "mixed",
                "meta": {"name": "dynamic-workflow-research-artifact", "description": "Research, create the requested artifact, and verify it"},
                "phases": [
                    {"title": "Research", "agents": [{
                        "label": "research-sources", "role": "research",
                        "prompt": f"Research the user's request using the required search capability. Task: {task_text}. Return a concise conclusion and write detailed sources/facts/uncertainty as JSON to {research_artifact_path} using file_write, then read it back with file_read. Do not create the final artifact.",
                        "actions": ["research", "persist_research"], "toolProfile": "research",
                        "capabilities": ["web_search", "file_write", "file_read"],
                        "writeScope": [research_artifact_path], "deliverables": [research_artifact_path],
                        "acceptanceChecks": ["required_capability_evidence", "artifact_exists", "artifact_readback"], "dependsOn": [],
                    }]},
                    {"title": "Create Artifact", "agents": [{
                        "label": "write-html", "role": "implementation",
                        "prompt": f"Using the research result, create a self-contained {artifact_kind} artifact at {artifact_path}. Use file_write, then read it back with file_read. Task: {task_text}",
                        "actions": ["create_artifact"], "toolProfile": "authoring",
                        "capabilities": ["file_write", "file_read"], "writeScope": [artifact_path],
                        "deliverables": [artifact_path], "acceptanceChecks": ["artifact_exists", "artifact_readback"],
                        "dependsOn": ["research-sources"],
                    }]},
                    {"title": "Verify Artifact", "agents": [{
                        "label": "verify-html", "role": "verification",
                        "prompt": f"Independently inspect {artifact_path}; verify it exists, is non-empty, and has valid {artifact_kind} structure. Return verificationPassed, checks, and blockingIssues. Do not modify the artifact.",
                        "actions": ["verify_artifact"], "toolProfile": "verify",
                        "capabilities": ["file_read"], "schemaRef": "VERIFICATION_SCHEMA", "strictSchema": True,
                        "deliverables": ["artifact_verification"], "acceptanceChecks": ["artifact_structure"],
                        "dependsOn": ["write-html"],
                    }]},
                ],
                "schemas": {"VERIFICATION_SCHEMA": GA_WORKFLOW_VERIFICATION_SCHEMA},
                "artifacts": [research_artifact_path, artifact_path, "artifact_verification"],
                "executionContract": {
                    "requiresExecution": True,
                    "actions": [
                        {"id": "research", "agent": "research-sources"},
                        {"id": "persist_research", "agent": "research-sources"},
                        {"id": "create_artifact", "agent": "write-html"},
                        {"id": "verify_artifact", "agent": "verify-html"},
                    ],
                    "capabilities": ["web_search", "file_write", "file_read"],
                    "requiredCapabilityEvidence": [
                        {"capability": "web_search", "agent": "research-sources", "minimumCalls": 1, "mode": "required"},
                        {"capability": "file_write", "agent": "research-sources", "minimumCalls": 1, "mode": "preferred"},
                    ],
                    "artifacts": [{
                        "path": research_artifact_path,
                        "writer": "research-sources",
                        "requiredChecks": ["artifact_exists", "artifact_readback"],
                    }, {
                        "path": artifact_path,
                        "writer": "write-html",
                        "requiredChecks": ["artifact_exists", "artifact_readback"],
                    }],
                },
                "acceptance": {"required": True, "failWorkflowOnError": True, "checks": []},
                "verification": {"level": "full", "checks": [
                    {"id": "artifact-exists", "kind": "artifact", "path": artifact_path, "required": True, "owner": "host"},
                ], "independentReview": True},
                "constraints": ["no_secret_files", "no_git_commit"],
            }
        if classification["taskType"] == "research":
            return {
                "taskType": "research",
                "meta": {
                    "name": "dynamic-workflow-research",
                    "description": "Research task with source discovery and synthesis",
                },
                "phases": [
                    {
                        "title": "Source Discovery",
                        "agents": [
                            {
                                "label": "source-discovery",
                                "prompt": (
                                    f"任务：{task_text}\n"
                                    "收集公开来源、关键 claims、风险和后续验证建议；"
                                    "按宿主声明的 JSON Schema 返回 sources、claims、risks 三个字段，"
                                    "不要只给自然语言摘要。"
                                ),
                                "actions": ["research"],
                                "toolProfile": "research",
                                "capabilities": ["web_search", "file_read"],
                                "schemaRef": "SOURCE_SCHEMA",
                                "dependsOn": [],
                            }
                        ],
                    },
                    {
                        "title": "Synthesis",
                        "agents": [
                            {
                                "label": "synthesis",
                                "prompt": "基于上游 Source Discovery 结果写中文综合报告，标注不确定性和建议。",
                                "actions": ["synthesis"],
                                "toolProfile": "planner",
                                "dependsOn": ["source-discovery"],
                            }
                        ],
                    },
                ],
                "executionContract": {
                    "requiresExecution": True,
                    "actions": [
                        {"id": "research", "agent": "source-discovery"},
                        {"id": "synthesis", "agent": "synthesis"},
                    ],
                    "capabilities": ["web_search"],
                    "requiredCapabilityEvidence": [
                        {"capability": "web_search", "agent": "source-discovery", "minimumCalls": 1, "mode": "required"},
                    ],
                },
                "schemas": {
                    "SOURCE_SCHEMA": {
                        "type": "object",
                        "required": ["sources", "claims", "risks"],
                        "properties": {
                            "sources": {"type": "array", "items": {"type": "object"}},
                            "claims": {"type": "array", "items": {"type": "object"}},
                            "risks": {"type": "array"},
                        },
                        "additionalProperties": True,
                    }
                },
                "artifacts": ["sources", "synthesis"],
                "constraints": ["no_secret_files", "no_git_commit"],
            }
        if classification["taskType"] == "coding":
            return {
                "taskType": "coding",
                "meta": {
                    "name": "dynamic-workflow-coding",
                    "description": "Coding task with TDD-ordered understand, tests, implementation, and verification phases",
                },
                "phases": [
                    {
                        "title": "Understand",
                        "agents": [
                            {
                                "label": "understand",
                                "role": "understanding",
                                "prompt": f"任务：{task_text}\n只读理解需求和相关文件，输出最小 TDD 切片建议；不要修改文件。",
                                "dependsOn": [],
                            }
                        ],
                    },
                    {
                        "title": "Tests",
                        "agents": [
                            {
                                "label": "write-tests",
                                "role": "tests",
                                "prompt": "基于理解结果先写或描述 failing tests，并明确如何看到红灯。",
                                "dependsOn": ["understand"],
                            }
                        ],
                    },
                    {
                        "title": "Implementation",
                        "agents": [
                            {
                                "label": "implement",
                                "role": "implementation",
                                "prompt": "在测试红灯后实现最小生产代码，使测试转绿。",
                                "dependsOn": ["write-tests"],
                            }
                        ],
                    },
                    {
                        "title": "Verification",
                        "agents": [
                            {
                                "label": "verify",
                                "role": "verification",
                                "prompt": "运行相关验证并返回结构化 verificationPassed、checks、blockingIssues；自然语言摘要不能替代验收证据。",
                                "schemaRef": "VERIFICATION_SCHEMA",
                                "strictSchema": True,
                                "dependsOn": ["implement"],
                            }
                        ],
                    },
                ],
                "schemas": {
                    "VERIFICATION_SCHEMA": {
                        "type": "object",
                        "required": ["verificationPassed", "checks", "blockingIssues"],
                        "properties": {
                            "verificationPassed": {"type": "boolean"},
                            "checks": {"type": "array"},
                            "blockingIssues": {"type": "array"},
                        },
                    }
                },
                "artifacts": ["understanding", "tests", "implementation", "verification"],
                "constraints": ["no_secret_files", "no_git_commit"],
                "acceptance": {
                    "required": True,
                    "failWorkflowOnError": True,
                    "checks": ["python_unittest", "verification_schema"],
                },
            }
        if classification["taskType"] == "general":
            # Explicit "/workflow <task>" whose shape the keyword classifier did
            # not recognise. The task must still be executed: the old fall-through
            # produced a single planner job, so the run "succeeded" without doing
            # any of the work the user asked for. The host resolves the declared
            # capability classes against the tools that are actually connected, so
            # this template never names a concrete tool, and it declares no
            # required artifact because the deliverable shape is unknown here.
            return {
                "taskType": "general",
                "meta": {
                    "name": "dynamic-workflow-general",
                    "description": "Execute the requested task, then verify the result",
                },
                "phases": [
                    {"title": "Execute", "agents": [{
                        "label": "execute-task",
                        "role": "implementation",
                        "prompt": (
                            f"任务：{task_text}\n"
                            "直接完成任务本身，不要只给计划、建议或提纲。"
                            "需要落盘的内容写入 workspace 内的相对路径，写完用 file_read 读回确认；"
                            "纯问答类任务直接把答案说清楚即可。"
                        ),
                        "actions": ["execute"],
                        "toolProfile": "authoring",
                        "capabilities": ["file_read", "file_write", "execute"],
                        "dependsOn": [],
                    }]},
                    {"title": "Verify", "agents": [{
                        "label": "verify-result",
                        "role": "verification",
                        "prompt": (
                            "独立核验上游执行结果：原始任务要求的每一项是否真的完成、产物是否存在且内容正确。"
                            "返回 verificationPassed、checks、blockingIssues；不要修改产物。"
                        ),
                        "actions": ["verify"],
                        "toolProfile": "verify",
                        "capabilities": ["file_read", "execute"],
                        "schemaRef": "VERIFICATION_SCHEMA",
                        "strictSchema": True,
                        "dependsOn": ["execute-task"],
                    }]},
                ],
                "schemas": {"VERIFICATION_SCHEMA": GA_WORKFLOW_VERIFICATION_SCHEMA},
                "artifacts": ["verification"],
                "constraints": ["no_secret_files", "no_git_commit"],
                # The execute agent carries the implementation role, so the host
                # holds this plan to the coding contract: one required,
                # host-evaluable check. That check is the verifier's structured
                # verdict. No test runner is declared, because the deliverable
                # shape is unknown here -- guessing python_unittest is exactly
                # the "research plan held to the coding gate" defect.
                "acceptance": {"required": True, "failWorkflowOnError": True, "checks": ["verification_schema"]},
            }
        return {
            "taskType": classification["taskType"],
            "meta": {
                "name": f"dynamic-workflow-{classification['taskType']}",
                "description": f"Dynamic workflow for {classification['taskType']} task",
            },
            "phases": [
                {
                    "title": "Plan",
                    "agents": [
                        {
                            "label": "planner",
                            "prompt": f"任务：{task_text}\n制定最小执行计划和验证建议。",
                            "dependsOn": [],
                        }
                    ],
                }
            ],
            "schemas": {},
            "artifacts": ["plan"],
            "constraints": ["no_secret_files", "no_git_commit"],
        }


class NativeWorkflowPlannerClient:
    """LLM planner client using llm.yaml profile (not mykey config keys).

    Prefer profile_name / binding_provider. legacy `config_name` is treated as a
    profile name when it does not look like a mykey *_config key.
    """

    def __init__(
        self,
        config_name: str | None = None,
        *,
        profile_name: str | None = None,
        binding_provider=None,
    ):
        self.profile_name = profile_name or config_name
        self.config_name = self.profile_name  # backward-compatible attribute for tests
        self.binding_provider = binding_provider
        self.raw_outputs: list[str] = []

    def complete(self, messages: list[dict]) -> dict:
        session = self._open_session()
        prompt = str((messages or [{}])[0].get("content") or "") + """

硬性输出要求：
- 只输出一个 JSON object。
- 不要 Markdown。
- 不要解释。
- 不要输出 JavaScript；只输出 WorkflowPlan JSON。
"""
        raw = "".join(str(chunk) for chunk in session.ask({"role": "user", "content": [{"type": "text", "text": prompt}]}))
        self.raw_outputs.append(raw)
        return parse_json_object(raw)

    def _open_session(self):
        from workflow_llm import make_session, resolve_binding

        binding = resolve_binding(
            profile_name=self.profile_name,
            binding_provider=self.binding_provider,
        )
        self.config_name = binding.profile_name
        self.profile_name = binding.profile_name
        return make_session(binding)


def parse_json_object(raw: str) -> dict:
    text = str(raw or "").strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    decoder = json.JSONDecoder()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start = text.find("{")
        if start >= 0:
            parsed, _ = decoder.raw_decode(text, idx=start)
            if isinstance(parsed, dict):
                return parsed
        raise


def resolve_session(config_name: str):
    """Open a planner session for a llm.yaml *profile* name.

    Legacy name kept for tests that patch ``workflow_planner.resolve_session``.
    """
    from workflow_llm import binding_from_env, binding_from_profile, make_session

    name = str(config_name or "").strip()
    if name and not name.endswith("_config") and not name.startswith("native_"):
        return make_session(binding_from_profile(name))
    # Fall back to env/active_profile rather than mykey.
    return make_session(binding_from_env())


def build_workflow_planner_from_env(
    *,
    profile_name: str | None = None,
) -> WorkflowPlanner | LLMWorkflowPlanner:
    """Build the workflow planner. The plan is always model-authored.

    Step-Code has no deterministic plan template: the model writes the
    orchestration script itself. A fixed template set cannot be a *dynamic*
    workflow -- it can only pick one of N shapes, so a task whose wording misses
    the templates' keywords silently loses the work it asked for (real case:
    wf_622266234f1345359d4e5f999758b922, where "/workflow 写3个python demo 并检验"
    produced a planner-only plan). ``GA_WORKFLOW_PLANNER_MODE=deterministic`` is
    therefore gone: an unset or unrecognised mode now means the LLM planner.

    :class:`WorkflowPlanner` survives only as the internal fallback used when the
    planner model errors, and that path marks the run ``degraded``
    (``planner_fallback_deterministic``) instead of pretending the template was
    a plan the model produced.
    """
    mode = str(os.environ.get("GA_WORKFLOW_PLANNER_MODE") or "prompt_guided").strip().lower()
    if mode == "deterministic":
        raise ValueError(
            "GA_WORKFLOW_PLANNER_MODE=deterministic was removed: a fixed template set is not a "
            "dynamic workflow. Unset it (or use prompt_guided/llm/real) to plan with the model."
        )
    chosen = (
        profile_name
        or os.environ.get("GA_WORKFLOW_LLM_PROFILE")
        or os.environ.get("GA_REAL_API_PROFILE")
        or os.environ.get("GA_WORKFLOW_PLANNER_CONFIG")
        or os.environ.get("GA_REAL_API_CONFIG")
        or ""
    ).strip()
    # Drop legacy mykey-style keys so we use yaml active_profile via client default.
    if chosen.endswith("_config") or chosen.startswith("native_"):
        chosen = ""
    try:
        repair_attempts = int(os.environ.get("GA_WORKFLOW_PLANNER_REPAIR_ATTEMPTS") or "1")
    except ValueError:
        repair_attempts = 1
    client_kwargs: dict[str, Any] = {}
    if chosen:
        client_kwargs["profile_name"] = chosen
    return LLMWorkflowPlanner(
        client=NativeWorkflowPlannerClient(**client_kwargs),
        fallback=WorkflowPlanner(),
        max_repair_attempts=repair_attempts,
    )


class PlannerResponseError(ValueError):
    """The planner model replied with text that is not a decodable JSON object.

    Distinct from a provider/transport failure: the model answered, we just
    cannot parse it, so the repair loop can show it the exact parse error.
    """


class LLMWorkflowPlanner:
    def __init__(self, *, client, fallback: WorkflowPlanner | None = None, max_repair_attempts: int = 2):
        self.client = client
        self.fallback = fallback or WorkflowPlanner()
        self.max_repair_attempts = max(0, int(max_repair_attempts))

    def plan(self, task_text: str, context: dict[str, Any] | None = None) -> WorkflowDraft:
        context = copy.deepcopy(context or {})
        repair_attempts: list[dict[str, Any]] = []
        plan: dict[str, Any] = {}
        validation: dict[str, Any] = {"ok": False, "issues": []}
        try:
            raw_plan: dict[str, Any] | None = self._request_plan(task_text, context, issues=[])
        except PlannerResponseError as exc:
            # The model replied, but not with JSON we can decode. That is a
            # repairable defect, not a provider outage: feed the exact parse
            # error back to the model instead of dropping straight to the
            # deterministic template, which degraded a run whose work would
            # otherwise have succeeded (real case: wf_a076100a22ef4e799fe2cd8dd81a027a).
            raw_plan = None
            validation = {
                "ok": False,
                "issues": [{"code": "planner_response_unparsable", "message": str(exc)}],
            }
        except Exception as exc:
            return self._fallback_draft(task_text, context, reason=str(exc))

        for _ in range(self.max_repair_attempts + 1):
            if raw_plan is not None:
                try:
                    plan = _normalize_plan_contract(raw_plan)
                    validation = validate_workflow_plan(plan)
                except (TypeError, ValueError, KeyError) as exc:
                    # A malformed contract is a validator issue, not a provider
                    # failure. Keep the raw plan so the normal repair prompt can
                    # show the model the exact contract defect instead of silently
                    # switching to the deterministic planner.
                    plan = copy.deepcopy(raw_plan) if isinstance(raw_plan, dict) else {}
                    validation = {
                        "ok": False,
                        "issues": [{"code": "invalid_plan_contract", "message": str(exc)}],
                    }
                if validation["ok"]:
                    classification = self.fallback.classify(task_text, context)
                    classification["taskType"] = str(plan.get("taskType") or classification["taskType"])
                    script = render_workflow_plan(plan)
                    context["plannerMode"] = "prompt_guided"
                    if repair_attempts:
                        context["repairAttempts"] = repair_attempts
                    return WorkflowDraft(task_text=task_text, context=context, classification=classification, plan=plan, validation=validation, script=script)
            if len(repair_attempts) >= self.max_repair_attempts:
                break
            repair_attempts.append({"issues": copy.deepcopy(validation["issues"]), "plan": copy.deepcopy(plan)})
            try:
                raw_plan = self._request_plan(
                    task_text,
                    context,
                    issues=validation["issues"],
                    previous_plan=plan,
                )
            except PlannerResponseError as exc:
                raw_plan = None
                validation = {
                    "ok": False,
                    "issues": [{"code": "planner_response_unparsable", "message": str(exc)}],
                }
            except Exception as exc:
                return self._fallback_draft(task_text, context, reason=str(exc))
        return self._rejected_draft(task_text, context, plan=plan, validation=validation, repair_attempts=repair_attempts)

    def _request_plan(
        self,
        task_text: str,
        context: dict[str, Any],
        *,
        issues: list[dict[str, Any]],
        previous_plan: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            response = self.client.complete([{"role": "system", "content": self._planner_prompt(task_text, context, issues=issues, previous_plan=previous_plan)}])
        except json.JSONDecodeError as exc:
            raise PlannerResponseError(str(exc)) from exc
        if isinstance(response, dict):
            return copy.deepcopy(response)
        if isinstance(response, str):
            try:
                return parse_json_object(response)
            except json.JSONDecodeError as exc:
                raise PlannerResponseError(str(exc)) from exc
        raise TypeError("planner client must return WorkflowPlan JSON as dict or JSON string")

    def _planner_prompt(
        self,
        task_text: str,
        context: dict[str, Any],
        *,
        issues: list[dict[str, Any]],
        previous_plan: dict[str, Any] | None,
    ) -> str:
        classification_hint = self.fallback.classify(task_text, context)
        # Generated, never hand-copied: a profile added to WORKFLOW_TOOL_PROFILES
        # or a capability class added to CAPABILITY_CLASSES must show up here
        # without a second edit (Step-Code renders its guidance from its tables).
        tool_profile_guidance = format_tool_profile_guidance("zh")
        role_profile_guidance = format_role_profile_guidance("zh")
        capability_classes = format_capability_classes("zh")
        prompt = {
            "role": "GA Workflow Planner",
            "task": task_text,
            "context": context,
            "classificationHint": classification_hint["taskType"],
            "contract": "Return WorkflowPlan JSON only. 不要输出 JS. Do not wrap in markdown. phases must be a non-empty array.",
            "orchestrationPolicy": [
                "根据任务语义和 classificationHint 规划 taskType、phase、agent、dependsOn、parallel-safe groups、schemas、artifacts；taskType 仅为提示，不触发硬门禁。",
                "任何会写出产物的计划（research 写证据文件、synthesis 写报告、coding 写源码都一样）都必须在 verification.checks 或 executionContract.artifacts[].requiredChecks 中声明至少一个宿主可求值的 required check，优先使用 artifact_exists / artifact_readback 这类机器检查；宿主会在 verification.checks 为空时从执行契约派生。只读的研究、审阅和规划任务不需要伪造验收项。写文件不等于写代码：是否套用 coding 拓扑规则只由 implementation/tests/repair 这些代码产出角色触发，role 本身是可选元数据。",
                "executionContract.artifacts 里声明的每个产物都会被宿主强制校验是否存在（不依赖你是否写出 artifact_exists），缺失即 run 失败。只有在产物确实是可有可无时才写 optional: true；不要用 optional 掩盖应该产出的交付物。",
                "verification.level 使用 none/inline/full；full 才需要 independentReview，且应显式声明 review agent 或独立 review capability。",
                "验收检查项由任务决定，可使用 command、schema、artifact 等受支持 kind；不要把 verification agent、verification_schema 或 python_unittest 当作所有代码任务的固定要求。",
                "小任务（单步、低风险、无需多 agent 协作）必须 mode=direct，不要为了简单问题创建 workflow。",
                "每个 agent 必须声明 bounded retryPolicy（maxAttempts 只能是 1-3），不得生成无界重试；transient/provider/MCP/schema 错误要区分 retryable 与 blocking。",
                "delegated mode（有界 sidecar）必须遵守 maxAgents<=5、maxWaves<=4，并在 approvalRequired=true 时停在 awaiting approval；普通 workflow 不受此限制，按任务真实复杂度拆分 phase/agent，宿主会按依赖图计算所需 wave 数。",
                "如果任务要求审查安全/性能/测试缺口/回归风险，taskType 必须是 review，不要误判为 research。",
                "如果任务要求规划设计方案或实施计划且明确不要直接写代码，taskType 应为 planning 或 mixed。",
                "review 任务按 security/performance/test-gap/regression 等独立维度 fan out。",
                "如果计划声明 role=tests，就必须产出与任务对应的测试证据；该 role 不能由 taskType 推断。",
                "对实际声明为代码产出的计划，role 如有填写必须使用 canonical role：understanding、contract、tests、implementation、verification、review、repair、summary、synthesis；不要使用 test-writer 等别名。",
                "research 任务可多来源并行，synthesis 必须依赖上游结果，并应包含 credibility/evidence 检查。依赖结果通过宿主 bounded dependency handoff 传递：只放短摘要、逻辑 resultRef、workspace-relative artifactRefs/handoffRef 和审计 transcriptRef，禁止在 prompt 或脚本中 JSON.stringify 上游完整结果/transcript。对大研究结果优先要求上游将详细证据写入 workspace-relative JSON/Markdown 产物，下游按路径 file_read。",
                "phases 必须至少包含一个 phase；每个 phase 必须至少包含一个 agent。",
                "When the user asks to perform work, produce executable action packets; 不要只返回计划。",
                "Artifact paths are workspace-relative only (for example tmp/report.html). Never emit /tmp, D:\tmp, ~, UNC, or .. traversal paths; the host hard-normalizes and rejects paths outside the launch workspace, so do not treat prompt text as permission.",
                "执行型计划必须声明 executionContract.requiresExecution、actions、capabilities 和 artifact acceptance checks，并把每个 action 映射到实际执行它的 agent。",
                "schemas 的 key 是不透明的 schemaRef 字符串，优先使用 SOURCE_SCHEMA、VERIFICATION_SCHEMA 等逻辑名称；不要把文件路径写入 schemaRef。无论 schemaRef 形状如何，必须保持与 schemas key 完全一致，schema 必须是纯 JSON Schema 数据而不是 JavaScript 代码。",
                "每个 schemaRef 的 schema 必须完整到子代理无需猜测：声明 type、properties（每个必须出现的字段都要有 type，数组要给 items 或至少 type=array）以及 required。只写 required 而不写 properties 会让子代理返回自然语言并按 schema 失败阻塞整条 run；不要依赖子代理自觉输出 JSON。",
                "声明了 schemaRef 的 agent 的 prompt 必须显式要求按该 schema 输出 JSON 字段，不要只写「返回结构化摘要」这类自然语言指令；宿主会把 schema 作为硬性输出契约校验。",
                "strictSchema 默认为 true 语义：声明了 schemaRef 的 agent 若 schema 校验失败即视为该 job 失败并阻塞 run。只有确实允许文本降级的 agent 才显式写 schemaPolicy: \"optional\"；此时降级会被宿主记为 degraded（部分交付）终态，而不是成功，所以不要为了省事批量声明 optional。",
                f"工具边界由宿主拥有，你不要猜测或指定具体工具名。每个 agent 用 toolProfile 声明档位（{tool_profile_guidance}；{role_profile_guidance}；orchestration 一律禁用），用 capabilities 声明需要的能力类别（{capability_classes}）。宿主会把档位解析成该 agent 实际可用的工具集合，并拒绝档位外的调用；能力在环境里不存在时 run 记 degraded，而不是失败。requiredCapabilityEvidence 只有 mode=required 才阻塞；web_search 默认 required，file_write/file_read 默认 preferred。artifact_exists 和 artifact_readback 是通用硬门禁；artifact_structure/source_count 默认只是 advisory 观察项，不要猜测 DOCX/HTML/PDF/图片内容。只有用户或计划明确声明 strict=true/contentValidation=true 时，才把内容结构/来源数量作为阻塞检查。DOCX 可使用 code_run + python-docx。",
                "action id must match the exact same string in the assigned agent.actions array; action ids are stable ids such as search, create_artifact, verify_artifact, never display labels or prose.",
                "Never write a concrete tool name (mcp__*, file_write, code_run, ...) in requiredTools or in a prompt as an instruction. Declare the profile and the capability classes instead; the host resolves them against the tools that are actually connected. requiredTools/requiredToolEvidence are accepted only for backward compatibility with older plans.",
                "acceptanceChecks and executionContract.artifacts[].requiredChecks must use machine-checkable ids only: artifact_exists, artifact_readback, artifact_structure, required_capability_evidence, required_tool_evidence, source_count, schema_valid, command_exit_zero, no_secret_pattern. Never put natural-language sentences in check arrays. Host artifact requiredChecks must match the writer checks; independent structure checks belong to the verifier agent.",
                "parallel is a phase barrier for independent work; pipeline expresses ordered dependencies without pretending all work is independent.",
                "Schema validation retries, retry counts, token/time budgets and max agents must be bounded; report terminal success/failure and blocking issues explicitly.",
                "Host preflights MCP/tool requirements and passes a capability snapshot; child agents must not probe local configuration or rediscover credentials.",
                "agent label 使用清晰英文短语，避免无意义缩写。",
            ],
            # Orchestration strategy, not contract mechanics. Step-Code's workflow
            # tool description teaches the model *which shape to build* (named
            # patterns, verification spend, sizing, no silent caps) before any
            # schema rules; GA's policy list above was almost entirely contract
            # bookkeeping, which is why plans came out as two or three agents in a
            # row instead of an adversarially verified DAG.
            "orchestrationPlaybook": [
                "先选模式再填字段。可用模式：多路并行调研后综合（research 并行 → synthesis 依赖汇总）；对抗验证（同一结论派多个不同视角的 skeptic，多数反驳即否决该结论）；评审团（同一问题多种解法，评审后综合）；穷尽式搜索（反复派 finder 直到连续 K 轮没有新发现）；多模态扫描（每个 agent 走不同检索路径：命名 / 结构 / 历史 / 文档）；完整性批评者（最后一个 agent 只回答『还缺什么』）。",
                "把 agent 花在验证上，而不是只花在生成上：对影响结论的关键主张安排独立验证，不要让产出者自证。",
                "规模默认中等：除非用户明确要求规模，保持整个 run 在约 15 个 agent 以内；一个 agent 能做完的不要拆成三个，也不要为了显得完整而堆 agent。",
                "No silent caps：任何被丢弃、截断、抽样、跳过的重试或 top-N 截断，都必须在产物或最终回答里显式记录，不允许静默丢信息。",
                "什么时候不要用 workflow：单文件改动、一次性查询、约三次检索就能答完的问题，用 mode=direct 直接做，不要为了用 workflow 而拆分。",
                "每个 agent 的 prompt 必须自带四件事：目标（要产出什么）、输入（上游短摘要 + 可 file_read 的 workspace-relative 路径）、输出契约（schemaRef 或明确字段）、完成与失败判据；不要写『分析一下 X』这类无边界指令。",
                "phase 标题按交付物命名（例如『来源收集』『对抗验证』『综合报告』），让 UI 和人类读者一眼看懂进度；不要用『Phase 1』这类无语义标题。",
            ],
            "requiredShape": {
                "taskType": "research | coding | review | debugging | planning | mixed",
                "meta": {"name": "...", "description": "..."},
                "phases": [{"title": "...", "agents": [{"label": "...", "role": "optional metadata; canonical values in codingAgentRoles, required to be canonical only inside code-producing plans", "prompt": "...", "toolProfile": "planner | research | authoring | verify | * (optional; host derives it from role when omitted)", "capabilities": ["web_search | web_fetch | file_read | file_write | execute (optional)"], "dependsOn": [], "schemaRef": "optional logical schema name", "strictSchema": "boolean, default true", "schemaPolicy": "optional; strict|optional, only meaningful together with schemaRef"}]}],
                "codingAgentRoles": sorted(CODING_AGENT_ROLES),
                "schemas": {},
                "artifacts": [],
                "executionContract": {
                    "requiresExecution": "boolean",
                    "actions": [{"id": "...", "agent": "..."}],
                    "capabilities": [format_capability_classes("en", " | ")],
                    "requiredCapabilityEvidence": [{"capability": "...", "agent": "...", "minimumCalls": 1, "mode": "required | preferred"}],
                    "artifacts": [{"path": "...", "writer": "...", "requiredChecks": ["..."], "optional": "boolean, default false; true exempts this artifact from host existence checking"}],
                },
                "constraints": ["no_secret_files", "no_git_commit"],
                "verification": {"level": "none | inline | full", "checks": [{"id": "...", "kind": "command | schema | artifact", "required": True, "owner": "host | <agent-label>"}], "independentReview": False},
            },
        }
        if issues:
            prompt["repair"] = {
                "validatorIssues": issues,
                "previousPlan": previous_plan,
                "repairRules": [
                    "Fix every listed issue in the JSON fields named by the validator; do not merely rewrite prompts or explain the issue.",
                    "Keep action ids identical between executionContract.actions and the assigned agent.actions.",
                    "Use only capability classes and machine-checkable acceptance ids from orchestrationPolicy; never invent concrete tool names.",
                    "Preserve valid phases and dependencies; return a complete replacement WorkflowPlan JSON object.",
                    "Keep the orchestrationPlaybook shape: do not satisfy the validator by silently deleting agents, checks or verification steps -- if you drop something, say so in the plan.",
                ],
            }
        return json.dumps(prompt, ensure_ascii=False, indent=2)

    def _fallback_draft(self, task_text: str, context: dict[str, Any], *, reason: str) -> WorkflowDraft:
        draft = self.fallback.plan(task_text, context)
        draft.context["plannerMode"] = "fallback_deterministic"
        draft.context["fallbackReason"] = reason
        draft.validation = copy.deepcopy(draft.validation)
        draft.validation["mode"] = "fallback_deterministic"
        draft.validation["fallbackReason"] = reason
        return draft

    def _rejected_draft(
        self,
        task_text: str,
        context: dict[str, Any],
        *,
        plan: dict[str, Any],
        validation: dict[str, Any],
        repair_attempts: list[dict[str, Any]],
    ) -> WorkflowDraft:
        classification = self.fallback.classify(task_text, context)
        classification["taskType"] = str(plan.get("taskType") or classification["taskType"])
        rejected_context = copy.deepcopy(context)
        rejected_context["plannerMode"] = "prompt_guided_rejected"
        if repair_attempts:
            rejected_context["repairAttempts"] = copy.deepcopy(repair_attempts)
        rejected_validation = copy.deepcopy(validation)
        rejected_validation["mode"] = "rejected"
        return WorkflowDraft(
            task_text=task_text,
            context=rejected_context,
            classification=classification,
            plan=copy.deepcopy(plan),
            validation=rejected_validation,
            script="",
        )


def validate_rendered_workflow_script(script: str) -> dict[str, Any]:
    """Compile a rendered workflow without executing any agent calls."""

    source = str(script or "")
    if not source.strip():
        return {"ok": False, "error": "workflow script is empty"}
    # Match workflow_js_worker.transformScript for syntax-only checking.
    source = source.replace("export const meta =", "const meta =", 1)
    newline = chr(10)
    wrapped = "(async () => {" + newline + source + newline + "})()" + newline
    executable = "node.exe" if sys.platform.startswith("win") else "node"
    try:
        completed = subprocess.run(
            [executable, "--check", "-"],
            input=wrapped,
            text=True,
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": f"workflow script syntax preflight unavailable: {exc}"}
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "syntax check failed").strip()
        return {"ok": False, "error": detail[:2000]}
    return {"ok": True}


def validate_workflow_plan(plan: dict[str, Any]) -> dict[str, Any]:
    issues: list[dict[str, str]] = []
    labels: set[str] = set()
    label_phase: dict[str, int] = {}
    for phase_index, phase in enumerate(plan.get("phases") or []):
        for agent in phase.get("agents") or []:
            label = str(agent.get("label") or "")
            if label:
                label_phase.setdefault(label, phase_index)
    dependency_graph: dict[str, list[str]] = {}
    raw_schemas = plan.get("schemas")
    schemas = raw_schemas if isinstance(raw_schemas, dict) else {}
    if raw_schemas is not None and not isinstance(raw_schemas, dict):
        issues.append({"code": "invalid_schema_registry", "message": "schemas must be a JSON object keyed by schemaRef"})
    if len(schemas) > 32:
        issues.append({"code": "schema_registry_too_large", "message": "schemas may contain at most 32 definitions"})
    total_schema_bytes = 0
    for schema_ref, schema in schemas.items():
        ref = str(schema_ref)
        if not ref.strip():
            issues.append({"code": "invalid_schema_ref", "message": "schemaRef keys must be non-empty strings"})
        if not isinstance(schema, (dict, bool)):
            issues.append({"code": "invalid_schema_definition", "message": f"schema {ref or '<empty>'} must be a JSON Schema object or boolean"})
            continue
        try:
            encoded_schema = json.dumps(schema, ensure_ascii=False, separators=(",", ":"))
        except (TypeError, ValueError):
            issues.append({"code": "invalid_schema_definition", "message": f"schema {ref or '<empty>'} is not JSON serializable"})
            continue
        total_schema_bytes += len(encoded_schema.encode("utf-8"))
        if len(encoded_schema.encode("utf-8")) > 64_000:
            issues.append({"code": "schema_definition_too_large", "message": f"schema {ref or '<empty>'} exceeds 64 KiB"})
    if total_schema_bytes > 256_000:
        issues.append({"code": "schema_registry_too_large", "message": "schema registry exceeds 256 KiB"})
    is_coding = plan_produces_code(plan)
    for phase_index, phase in enumerate(plan.get("phases") or []):
        phase_title = str(phase.get("title") or "")
        phase_labels: set[str] = set()
        phase_roles = {str(agent.get("role") or "").strip().lower() for agent in phase.get("agents") or []}
        if is_coding and "tests" in phase_roles and "implementation" in phase_roles:
            issues.append({"code": "coding_tests_parallel_implementation", "message": f"coding phase {phase_title} parallelizes tests and implementation"})
        for agent in phase.get("agents") or []:
            label = str(agent.get("label") or "")
            if not label:
                issues.append({"code": "missing_label", "message": f"phase {phase_title} has agent without label"})
                continue
            if label in labels or label in phase_labels:
                issues.append({"code": "duplicate_label", "message": f"duplicate agent label: {label}"})
            phase_labels.add(label)
            role = str(agent.get("role") or "").strip().lower()
            # Role is optional metadata, not a task-type gate. Coding topology is
            # activated by an agent declaring a code-producing role, never by the
            # plan writing artifacts: a research/report plan writing files is not
            # a coding plan, and must not flip contracts depending on whether the
            # model happened to fill in ``role``.
            #
            # Inside a coding plan a role that is present must still be canonical.
            # Accepting an alias such as ``test-writer`` as "not a coding role"
            # would silently skip the tests/implementation topology check, which
            # is exactly the bypass this guard exists to stop. An omitted role
            # stays legal; a misspelled one does not.
            if is_coding and role and role not in CODING_AGENT_ROLES:
                issues.append({"code": "invalid_coding_role", "message": f"coding agent {label} has non-canonical role: {role}"})
            declared_profile = agent.get("toolProfile")
            if declared_profile is not None and not is_known_tool_profile(declared_profile):
                issues.append({
                    "code": "unknown_tool_profile",
                    "message": f"agent {label} declares unknown toolProfile {declared_profile!r}; the host owns the profile list",
                })
            declared_capabilities = agent.get("capabilities")
            if declared_capabilities is not None:
                if not isinstance(declared_capabilities, list):
                    issues.append({"code": "invalid_capability_list", "message": f"agent {label} capabilities must be a list"})
                else:
                    for capability in declared_capabilities:
                        if str(capability) not in CAPABILITY_CLASSES:
                            issues.append({
                                "code": "unknown_capability",
                                "message": f"agent {label} declares unknown capability {capability!r}; known: {', '.join(CAPABILITY_CLASSES)}",
                            })
            schema_ref = agent.get("schemaRef")
            if schema_ref is not None:
                if not isinstance(schema_ref, str) or not schema_ref.strip():
                    issues.append({"code": "invalid_schema_ref", "message": f"agent {label} schemaRef must be a non-empty string"})
                elif schema_ref not in schemas:
                    issues.append({"code": "undefined_schema", "message": f"agent {label} references undefined schema: {schema_ref}"})
            schema_policy = agent.get("schemaPolicy")
            if schema_policy is not None:
                normalized_policy = str(schema_policy).strip().lower()
                if normalized_policy not in {"strict", "optional"}:
                    issues.append({"code": "invalid_schema_policy", "message": f"agent {label} schemaPolicy must be one of strict|optional, got: {schema_policy}"})
                elif not (isinstance(schema_ref, str) and schema_ref.strip()):
                    issues.append({"code": "schema_policy_without_schema", "message": f"agent {label} declares schemaPolicy={normalized_policy} without schemaRef"})
            dependencies = agent.get("dependsOn") or []
            dependency_graph[label] = [str(item) for item in dependencies if str(item) in label_phase]
            if len(dependencies) != len(set(str(item) for item in dependencies)):
                issues.append({"code": "duplicate_dependency", "message": f"agent {label} declares the same dependency more than once"})
            for dependency in dependencies:
                dependency = str(dependency)
                if dependency not in label_phase:
                    issues.append({"code": "undefined_dependency", "message": f"agent {label} depends on undefined or same-phase label: {dependency}"})
                elif label_phase[dependency] == phase_index:
                    issues.append({"code": "same_phase_dependency", "message": f"agent {label} depends on same-phase agent: {dependency}"})
                elif label_phase[dependency] > phase_index:
                    issues.append({"code": "forward_dependency", "message": f"agent {label} depends on later-phase agent: {dependency}"})
        labels.update(phase_labels)
    if not plan.get("phases"):
        issues.append({"code": "missing_phase", "message": "workflow plan requires at least one phase"})

    execution_contract = plan.get("executionContract")
    if isinstance(execution_contract, dict) and execution_contract.get("requiresExecution") is True:
        agents_by_label = {
            str(agent.get("label") or ""): agent
            for phase in (plan.get("phases") or [])
            for agent in (phase.get("agents") or [])
            if str(agent.get("label") or "")
        }
        actionable_roles = {"research", "implementation", "tests", "verification", "review", "repair", "synthesis"}
        actionable_agents = [
            agent for agent in agents_by_label.values()
            if str(agent.get("role") or "").strip().lower() in actionable_roles
            or agent.get("actions")
            or agent.get("requiredTools")
            or agent.get("capabilities")
            or agent.get("writeScope")
        ]
        if not actionable_agents:
            issues.append({"code": "plan_only_execution", "message": "execution contract requires at least one executable non-planner agent"})

        actions = execution_contract.get("actions") or []
        if not actions:
            issues.append({"code": "missing_execution_actions", "message": "execution contract requires explicit action mappings"})
        mapped_actions = {
            str(action.get("id") or "")
            for action in actions
            if isinstance(action, dict) and str(action.get("id") or "")
        }
        declared_actions = {
            str(action)
            for agent in agents_by_label.values()
            for action in (agent.get("actions") or [])
            if str(action)
        }
        for action_id in sorted(declared_actions - mapped_actions):
            issues.append({"code": "unmapped_agent_action", "message": f"agent action is missing from execution contract: {action_id}"})
        for action in actions:
            action_id = str(action.get("id") or "") if isinstance(action, dict) else ""
            agent_label = str(action.get("agent") or "") if isinstance(action, dict) else ""
            agent = agents_by_label.get(agent_label)
            if not action_id or not agent:
                issues.append({"code": "invalid_action_mapping", "message": f"execution action {action_id or '<missing>'} references an unknown agent"})
            elif action_id not in {str(item) for item in (agent.get("actions") or [])}:
                issues.append({"code": "action_not_declared_by_agent", "message": f"agent {agent_label} does not declare action {action_id}"})

        declared_tools = {str(item) for item in execution_contract.get("requiredTools") or []}
        agent_tools = {str(tool) for agent in agents_by_label.values() for tool in (agent.get("requiredTools") or [])}
        for tool in sorted(declared_tools - agent_tools):
            issues.append({"code": "missing_required_tool", "message": f"required tool is not assigned to an agent: {tool}"})

        for evidence in execution_contract.get("requiredToolEvidence") or []:
            if not isinstance(evidence, dict):
                issues.append({"code": "invalid_tool_evidence_contract", "message": "tool evidence contract must be an object"})
                continue
            tool = str(evidence.get("tool") or "")
            agent_label = str(evidence.get("agent") or "")
            mode = str(evidence.get("mode") or "required").strip().lower()
            minimum_calls = evidence.get("minimumCalls", 1)
            agent = agents_by_label.get(agent_label)
            try:
                minimum_calls = int(minimum_calls)
            except (TypeError, ValueError):
                minimum_calls = 0
            if mode not in {"required", "preferred", "informational"}:
                issues.append({"code": "invalid_tool_evidence_contract", "message": f"unsupported tool evidence mode: {mode or '<missing>'}"})
            if not tool or tool not in declared_tools or not agent or tool not in {str(item) for item in (agent.get("requiredTools") or [])} or minimum_calls < 1:
                issues.append({"code": "invalid_tool_evidence_contract", "message": f"tool evidence must bind a required tool to its executing agent: {tool or '<missing>'}"})

        for capability_evidence in execution_contract.get("requiredCapabilityEvidence") or []:
            if not isinstance(capability_evidence, dict):
                issues.append({"code": "invalid_capability_evidence_contract", "message": "capability evidence contract must be an object"})
                continue
            capability = str(capability_evidence.get("capability") or "")
            agent_label = str(capability_evidence.get("agent") or "")
            mode = str(capability_evidence.get("mode") or "required").strip().lower()
            agent = agents_by_label.get(agent_label)
            declared = {str(item) for item in (agent.get("capabilities") or [])} if agent else set()
            try:
                minimum_calls = int(capability_evidence.get("minimumCalls", 1))
            except (TypeError, ValueError):
                minimum_calls = 0
            if mode not in {"required", "preferred", "informational"}:
                issues.append({"code": "invalid_capability_evidence_contract", "message": f"unsupported capability evidence mode: {mode or '<missing>'}"})
            if capability not in CAPABILITY_CLASSES or not agent or capability not in declared or minimum_calls < 1:
                issues.append({
                    "code": "invalid_capability_evidence_contract",
                    "message": f"capability evidence must bind a declared capability to its executing agent: {capability or '<missing>'}",
                })

        for artifact in execution_contract.get("artifacts") or []:
            if not isinstance(artifact, dict):
                issues.append({"code": "invalid_artifact_contract", "message": "execution artifact contract must be an object"})
                continue
            artifact_path = str(artifact.get("path") or "").replace("\\", "/").strip("/")
            writer_label = str(artifact.get("writer") or "")
            writer = agents_by_label.get(writer_label)
            if not artifact_path or not writer:
                issues.append({"code": "invalid_artifact_contract", "message": f"artifact {artifact_path or '<missing>'} has no valid writer"})
                continue
            write_scopes = [str(item).replace("\\", "/").strip("/") for item in writer.get("writeScope") or []]
            deliverables = [str(item).replace("\\", "/").strip("/") for item in writer.get("deliverables") or []]
            if not any(artifact_path == scope or artifact_path.startswith(scope.rstrip("/") + "/") for scope in write_scopes if scope):
                issues.append({"code": "missing_artifact_write_scope", "message": f"writer {writer_label} has no write scope covering {artifact_path}"})
            if artifact_path not in deliverables:
                issues.append({"code": "missing_artifact_deliverable", "message": f"writer {writer_label} does not declare artifact deliverable {artifact_path}"})
            required_checks = {str(item) for item in artifact.get("requiredChecks") or []}
            # A non-optional artifact needs no model-authored check: the host
            # enforces existence by contract. Only an explicitly optional
            # artifact without checks is a genuine "nothing is verified here"
            # contract hole worth surfacing to the planner.
            if not required_checks and artifact.get("optional"):
                issues.append({"code": "missing_artifact_acceptance_check", "message": f"optional artifact {artifact_path} declares no acceptance check, so nothing is verified"})
            unsupported_checks = required_checks - ARTIFACT_ACCEPTANCE_CHECKS
            if unsupported_checks:
                issues.append({"code": "unsupported_artifact_acceptance_check", "message": f"artifact {artifact_path} has non-machine-checkable checks: {', '.join(sorted(unsupported_checks))}"})
            agent_checks = {str(item) for item in writer.get("acceptanceChecks") or []}
            if required_checks - agent_checks:
                issues.append({"code": "missing_artifact_acceptance_check", "message": f"writer {writer_label} does not declare checks: {', '.join(sorted(required_checks - agent_checks))}"})

    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(label: str) -> bool:
        if label in visiting:
            return True
        if label in visited:
            return False
        visiting.add(label)
        for dependency in dependency_graph.get(label, []):
            if visit(dependency):
                return True
        visiting.remove(label)
        visited.add(label)
        return False

    if any(visit(label) for label in dependency_graph if label not in visited):
        issues.append({"code": "dependency_cycle", "message": "workflow dependency graph contains a cycle"})
    try:
        verification_contract = normalize_verification_contract(plan)
    except ValueError as exc:
        verification_contract = {"level": "none", "checks": [], "independentReview": False}
        issues.append({"code": "invalid_verification_contract", "message": str(exc)})
    required_checks = [check for check in verification_contract.get("checks", []) if check.get("required") is True]
    if is_coding:
        if not required_checks:
            issues.append({
                "code": "missing_verification_check",
                "message": "coding workflow requires at least one required observable verification check",
            })
        if verification_contract.get("independentReview"):
            review_agents = [
                agent
                for phase in (plan.get("phases") or [])
                for agent in (phase.get("agents") or [])
                if str(agent.get("role") or "").strip().lower() in {"verification", "review"}
            ]
            if not review_agents:
                issues.append({
                    "code": "missing_independent_review_agent",
                    "message": "full verification contract requires an independent review capability or agent",
                })
    elif plan_declares_writes(plan) and not required_checks:
        # A plan that persists artifacts owes the host something deterministic to
        # evaluate. This replaces a task-type gate with a contract gate: the
        # requirement is "declare an evaluable check for what you write", and it
        # applies identically to research, review and coding plans. Model-authored
        # required checks are always honored above; the normalizer derives them
        # from ``executionContract.artifacts[].requiredChecks`` when the plan
        # leaves the verification view empty.
        issues.append({
            "code": "missing_verification_check",
            "message": "write-capable workflow requires at least one host-evaluable required check",
        })
    return {"ok": not issues, "issues": issues}


def render_workflow_plan(plan: dict[str, Any]) -> str:
    lines: list[str] = []
    meta = plan.get("meta") or {}
    phases = [{"title": phase.get("title")} for phase in plan.get("phases") or []]
    lines.append("export const meta = " + json.dumps({"name": meta.get("name"), "description": meta.get("description"), "phases": phases}, ensure_ascii=False, indent=2))
    lines.append("")
    schemas = plan.get("schemas") or {}
    if schemas:
        # Schema refs are model-produced data and may contain paths, spaces,
        # Unicode, or punctuation. Keep them as object keys instead of
        # interpolating them into JavaScript identifiers.
        lines.append(
            "const __workflowSchemas = Object.freeze("
            + json.dumps(schemas, ensure_ascii=False, indent=2)
            + ")"
        )
        lines.append("")
    result_names: list[str] = []
    rendered_labels: dict[str, str] = {}
    used_identifiers: set[str] = set()
    identifier_counter = 0
    for phase in plan.get("phases") or []:
        title = str(phase.get("title") or "")
        lines.append(f"phase('{_js_string(title)}')")
        agents = phase.get("agents") or []
        independent_agents = [agent for agent in agents if not (agent.get("dependsOn") or [])]
        if len(agents) > 1 and len(independent_agents) == len(agents):
            phase_vars = [
                _js_identifier(str(agent.get("label") or "agent"), index=identifier_counter + offset, used=used_identifiers)
                for offset, agent in enumerate(agents)
            ]
            identifier_counter += len(agents)
            lines.append(f"const [{', '.join(phase_vars)}] = await parallel([")
            for agent in agents:
                label = str(agent.get("label") or "agent")
                prompt = str(agent.get("prompt") or "")
                options = {"label": label, "phase": title}
                for key in ("actions", "requiredTools", "capabilities", "toolProfile", "writeScope", "deliverables", "acceptanceChecks", "capabilityProfile"):
                    if agent.get(key):
                        options[key] = agent[key]
                if agent.get("role"):
                    options["role"] = str(agent["role"])
                if agent.get("retryPolicy"):
                    options["retryPolicy"] = agent["retryPolicy"]
                if agent.get("schemaRef"):
                    options["schema"] = {"__schema_ref__": agent["schemaRef"]}
                    if agent.get("strictSchema"):
                        options["strictSchema"] = True
                    elif str(agent.get("schemaPolicy") or "").strip().lower() == "optional":
                        options["fallback"] = str(agent.get("fallback") or "text")
                lines.append(f"  () => agent(`{_template_string(prompt)}`, {_render_options(options)}),")
            for position, agent in enumerate(agents):
                rendered_labels[str(agent.get("label") or "agent")] = phase_vars[position]
            lines.append("])")
            result_names.extend(phase_vars)
            lines.append("")
            continue
        phase_vars: list[str] = []
        for agent in agents:
            label = str(agent.get("label") or "agent")
            var_name = _js_identifier(label, index=identifier_counter, used=used_identifiers)
            identifier_counter += 1
            prompt = str(agent.get("prompt") or "")
            dependencies = [rendered_labels[item] for item in agent.get("dependsOn") or [] if item in rendered_labels]
            rendered_prompt = _template_string(prompt)
            if dependencies:
                # Upstream payloads can contain large transcripts. The host scheduler
                # injects a bounded dependency handoff (summary + logical refs).
                rendered_prompt += '\n\n依赖结果由宿主以 bounded dependency handoff 提供；请根据摘要和产物路径按需读取，不要期待完整 transcript。'
            options = {"label": label, "phase": title}
            for key in ("actions", "requiredTools", "capabilities", "toolProfile", "writeScope", "deliverables", "acceptanceChecks", "capabilityProfile"):
                if agent.get(key):
                    options[key] = agent[key]
            if agent.get("role"):
                options["role"] = str(agent["role"])
            if agent.get("retryPolicy"):
                options["retryPolicy"] = agent["retryPolicy"]
            if agent.get("dependsOn"):
                options["dependsOn"] = [str(item) for item in agent["dependsOn"]]
            if agent.get("schemaRef"):
                options["schema"] = {"__schema_ref__": agent["schemaRef"]}
                if agent.get("strictSchema"):
                    options["strictSchema"] = True
                elif str(agent.get("schemaPolicy") or "").strip().lower() == "optional":
                    options["fallback"] = str(agent.get("fallback") or "text")
            lines.append(f"const {var_name} = await agent(`{rendered_prompt}`, {_render_options(options)})")
            phase_vars.append(var_name)
            rendered_labels[label] = var_name
        result_names.extend(phase_vars)
        lines.append("")
    acceptance = plan.get("acceptance") or {}
    acceptance_checks = acceptance.get("checks") if isinstance(acceptance, dict) else []
    if (
        plan_produces_code(plan)
        and isinstance(acceptance_checks, list)
        and any(
            (check.get("type") if isinstance(check, dict) else check) == "python_unittest"
            for check in acceptance_checks
        )
    ):
        lines.append("const __acceptanceGate = await runPythonUnittest(args.workspacePath, { pattern: 'test_*.py', phase: 'Verification', gateKey: 'workflow-acceptance' })")
        lines.append("")
    if result_names:
        lines.append("return { " + ", ".join(result_names) + " }")
    else:
        lines.append("return {}")
    return "\n".join(lines)


# Names the generated script binds at top level (runtime functions, script
# arguments, and JS keywords). A packet label that sanitizes onto one of these
# would shadow it -- ``const agent = await agent(...)`` is a TDZ ReferenceError,
# and ``const const = ...`` is a SyntaxError -- so they are never used as-is.
_JS_RESERVED_IDENTIFIERS = frozenset(
    {
        "agent",
        "parallel",
        "__workflowSchemas",
        "phase",
        "runPythonUnittest",
        "args",
        "meta",
        "JSON",
        "await",
        "async",
        "break",
        "case",
        "catch",
        "class",
        "const",
        "continue",
        "debugger",
        "default",
        "delete",
        "do",
        "else",
        "enum",
        "export",
        "extends",
        "false",
        "finally",
        "for",
        "function",
        "if",
        "implements",
        "import",
        "in",
        "instanceof",
        "interface",
        "let",
        "new",
        "null",
        "package",
        "private",
        "protected",
        "public",
        "return",
        "static",
        "super",
        "switch",
        "this",
        "throw",
        "true",
        "try",
        "typeof",
        "var",
        "void",
        "while",
        "with",
        "yield",
    }
)


def _js_identifier(label: str, *, index: int = 0, used: set[str] | None = None) -> str:
    """Map an agent label to a unique, valid JavaScript identifier.

    Non-ASCII labels (a Chinese review plan, say) all collapse to the empty
    string under ASCII sanitization. Falling back to a shared constant such as
    ``agent`` made two such agents emit ``const [agent, agent] = await
    parallel([...])``, a hard ``SyntaxError: Identifier 'agent' has already
    been declared`` that took the whole run down. The fallback is therefore
    positional, and a uniqueness guard covers sanitized near-collisions.
    """

    parts = re.sub(r"[^0-9A-Za-z_]+", "_", str(label)).strip("_")
    if not parts:
        parts = f"agent_{index + 1}"
    if parts[0].isdigit():
        parts = "agent_" + parts
    if parts in _JS_RESERVED_IDENTIFIERS:
        parts = f"{parts}_packet"
    if used is not None:
        base = parts
        suffix = 2
        while parts in used:
            parts = f"{base}_{suffix}"
            suffix += 1
        used.add(parts)
    return parts


def _js_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _template_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")


def _render_options(options: dict[str, Any]) -> str:
    parts: list[str] = []
    for key, value in options.items():
        if isinstance(value, dict) and value.get("__schema_ref__"):
            schema_ref = str(value["__schema_ref__"])
            parts.append(
                f"{key}: __workflowSchemas[{json.dumps(schema_ref, ensure_ascii=False)}]"
            )
        elif isinstance(value, (dict, list)):
            parts.append(f"{key}: {json.dumps(value, ensure_ascii=False, separators=(',', ':'))}")
        elif isinstance(value, bool):
            parts.append(f"{key}: {'true' if value else 'false'}")
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            parts.append(f"{key}: {value}")
        else:
            parts.append(f"{key}: '{_js_string(str(value))}'")
    return "{ " + ", ".join(parts) + " }"
