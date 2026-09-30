from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any

from workflow_policy import normalize_delegation_policy


CODING_AGENT_ROLES = frozenset(
    {
        "understanding",
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


def _normalize_workflow_execution_contract(plan: dict[str, Any]) -> dict[str, Any]:
    """Add a stable, inspectable orchestration contract to model-produced plans."""

    normalized = copy.deepcopy(plan)
    phases = normalized.get("phases") or []
    task_type = str(normalized.get("taskType") or "planning").strip().lower()
    mode = str(normalized.get("mode") or "").strip().lower()
    if mode not in WORKFLOW_MODES:
        mode = "workflow" if phases else "direct"
    risk_level = str(normalized.get("riskLevel") or "").strip().lower()
    if risk_level not in WORKFLOW_RISK_LEVELS:
        risk_level = "high" if task_type in {"coding", "debugging", "mixed"} else "medium" if task_type == "review" else "low"

    acceptance = normalized.get("acceptance") if isinstance(normalized.get("acceptance"), dict) else {}
    acceptance_checks = [
        str(check.get("type") if isinstance(check, dict) else check).strip()
        for check in acceptance.get("checks") or []
        if str(check.get("type") if isinstance(check, dict) else check).strip()
    ]
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
    eval_contract = {
        "level": str(eval_contract.get("level") or ("full" if task_type in {"coding", "debugging", "mixed"} else "inline")),
        "outcome": str(eval_contract.get("outcome") or "workflow completes with evidence-backed acceptance result"),
        "sharedSurfaces": list(eval_contract.get("sharedSurfaces") or []),
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

    normalized["workflowContractVersion"] = int(normalized.get("workflowContractVersion") or 1)
    normalized["mode"] = mode
    normalized["riskLevel"] = risk_level
    normalized["successCriteria"] = [str(item) for item in success_criteria]
    normalized["evalContract"] = eval_contract
    normalized["orchestration"] = orchestration
    for phase in phases:
        for agent in phase.get("agents") or []:
            agent.setdefault("owner", "workflow")
            agent.setdefault("writeScope", [])
            agent.setdefault("deliverables", [])
            agent["retryPolicy"] = _normalize_plan_retry_policy(agent.get("retryPolicy"))
    return normalized


def _normalize_coding_acceptance_contract(plan: dict[str, Any]) -> dict[str, Any]:
    """Normalize model-produced coding plans to GA's strict verification contract."""

    normalized = copy.deepcopy(plan)
    if normalized.get("taskType") != "coding":
        return normalized

    schemas = normalized.get("schemas")
    if not isinstance(schemas, dict):
        schemas = {}
        normalized["schemas"] = schemas

    required_fields = set(GA_WORKFLOW_VERIFICATION_SCHEMA["required"])
    for phase in normalized.get("phases") or []:
        for agent in phase.get("agents") or []:
            if str(agent.get("role") or "").strip().lower() != "verification":
                continue

            schema_ref = agent.get("schemaRef")
            existing_schema = schemas.get(schema_ref) if schema_ref else None
            existing_required = set(existing_schema.get("required") or []) if isinstance(existing_schema, dict) else set()
            has_complete_schema = (
                agent.get("strictSchema") is True
                and isinstance(existing_schema, dict)
                and required_fields.issubset(existing_required)
            )
            if has_complete_schema:
                continue

            if not (schema_ref and isinstance(existing_schema, dict) and required_fields.issubset(existing_required)):
                schema_ref = "GA_WORKFLOW_VERIFICATION_SCHEMA"
                schemas[schema_ref] = copy.deepcopy(GA_WORKFLOW_VERIFICATION_SCHEMA)
            agent["schemaRef"] = schema_ref
            agent["strictSchema"] = True
            prompt = str(agent.get("prompt") or "")
            requirement = (
                "必须返回严格 JSON，字段为 verificationPassed（布尔值）、checks（数组）、"
                "blockingIssues（数组）；不得用自然语言摘要替代结构化验收结果。"
            )
            if requirement not in prompt:
                agent["prompt"] = f"{prompt}\n{requirement}".strip()

    return normalized


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
        if any(word in text for word in ("调研", "研究", "资料", "来源")) or any(word in lowered for word in ("research", "source")):
            task_type = "research"
            read_write_mode = "read_only"
            needs_code_change = False
        elif any(word in text for word in ("实现", "修复", "开发", "修改")) or any(word in lowered for word in ("implement", "fix", "code")):
            task_type = "coding"
            read_write_mode = "may_write"
            needs_code_change = True
        elif any(word in text for word in ("审查", "评审", "review")) or "review" in lowered:
            task_type = "review"
            read_write_mode = "read_only"
            needs_code_change = False
        else:
            task_type = "planning"
            read_write_mode = "read_only"
            needs_code_change = False
        return {
            "taskType": task_type,
            "readWriteMode": read_write_mode,
            "needsMcp": False,
            "needsCodeChange": needs_code_change,
            "needsVerification": True,
            "riskLevel": "medium" if needs_code_change else "low",
            "clarifyingQuestions": [],
            "constraints": list((context or {}).get("constraints") or []),
        }

    def _build_plan(self, task_text: str, context: dict[str, Any], classification: dict[str, Any]) -> dict[str, Any]:
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
                                "prompt": f"任务：{task_text}\n收集公开来源、关键 claims、风险和后续验证建议，返回结构化摘要。",
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
                                "dependsOn": ["source-discovery"],
                            }
                        ],
                    },
                ],
                "schemas": {
                    "SOURCE_SCHEMA": {
                        "type": "object",
                        "required": ["sources", "claims", "risks"],
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
    mode = str(os.environ.get("GA_WORKFLOW_PLANNER_MODE") or "deterministic").strip().lower()
    if mode not in {"prompt_guided", "llm", "real"}:
        return WorkflowPlanner()
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


class LLMWorkflowPlanner:
    def __init__(self, *, client, fallback: WorkflowPlanner | None = None, max_repair_attempts: int = 2):
        self.client = client
        self.fallback = fallback or WorkflowPlanner()
        self.max_repair_attempts = max(0, int(max_repair_attempts))

    def plan(self, task_text: str, context: dict[str, Any] | None = None) -> WorkflowDraft:
        context = copy.deepcopy(context or {})
        try:
            plan = _normalize_workflow_execution_contract(
                _normalize_coding_acceptance_contract(self._request_plan(task_text, context, issues=[]))
            )
            repair_attempts: list[dict[str, Any]] = []
            for _ in range(self.max_repair_attempts + 1):
                validation = validate_workflow_plan(plan)
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
                plan = _normalize_workflow_execution_contract(
                    _normalize_coding_acceptance_contract(
                        self._request_plan(task_text, context, issues=validation["issues"], previous_plan=plan)
                    )
                )
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
        response = self.client.complete([{"role": "system", "content": self._planner_prompt(task_text, context, issues=issues, previous_plan=previous_plan)}])
        if isinstance(response, dict):
            return copy.deepcopy(response)
        if isinstance(response, str):
            return json.loads(response)
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
        prompt = {
            "role": "GA Workflow Planner",
            "task": task_text,
            "context": context,
            "classificationHint": classification_hint["taskType"],
            "contract": "Return WorkflowPlan JSON only. 不要输出 JS. Do not wrap in markdown. phases must be a non-empty array.",
            "orchestrationPolicy": [
                "根据任务语义和 classificationHint 决定 taskType、phase、agent、dependsOn、parallel-safe groups、schemas、artifacts。",
                "每个 agent 必须声明 bounded retryPolicy（maxAttempts 只能是 1-3），不得生成无界重试；transient/provider/MCP/schema 错误要区分 retryable 与 blocking。",
                "delegated mode 必须遵守 maxAgents<=5、maxWaves<=4，并在 approvalRequired=true 时停在 awaiting approval。",
                "如果任务要求审查安全/性能/测试缺口/回归风险，taskType 必须是 review，不要误判为 research。",
                "如果任务要求规划设计方案或实施计划且明确不要直接写代码，taskType 应为 planning 或 mixed。",
                "review 任务按 security/performance/test-gap/regression 等独立维度 fan out。",
                "coding 任务必须遵守 Understand -> Tests -> Implementation -> Verification，禁止 tests 与 implementation 并行。",
                "coding 任务的每个 agent.role 都是必填字段，只能使用 canonical role：understanding、contract、tests、implementation、verification、review、repair、summary、synthesis；不要省略 role 或使用 test-writer 等别名。",
                "coding 任务必须提供 acceptance.required=true 和非空 acceptance.checks；至少包含 verification_schema 或 python_unittest。",
                "verification agent 必须返回 verificationPassed、checks、blockingIssues；verification schema 不匹配时不得降级为普通文本。",
                "research 任务可多来源并行，synthesis 必须依赖上游结果，并应包含 credibility/evidence 检查。",
                "phases 必须至少包含一个 phase；每个 phase 必须至少包含一个 agent。",
                "agent label 使用清晰英文短语，避免无意义缩写。",
            ],
            "requiredShape": {
                "taskType": "research | coding | review | debugging | planning | mixed",
                "meta": {"name": "...", "description": "..."},
                "phases": [{"title": "...", "agents": [{"label": "...", "role": "required for coding; see codingAgentRoles", "prompt": "...", "dependsOn": []}]}],
                "codingAgentRoles": sorted(CODING_AGENT_ROLES),
                "schemas": {},
                "artifacts": [],
                "constraints": ["no_secret_files", "no_git_commit"],
                "acceptance": {"required": True, "failWorkflowOnError": True, "checks": ["verification_schema", "python_unittest"]},
            },
        }
        if issues:
            prompt["repair"] = {"validatorIssues": issues, "previousPlan": previous_plan}
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
    schemas = plan.get("schemas") or {}
    is_coding = plan.get("taskType") == "coding"
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
            if is_coding and not role:
                issues.append({"code": "missing_coding_role", "message": f"coding agent {label} must declare role"})
            elif is_coding and role not in CODING_AGENT_ROLES:
                issues.append({"code": "invalid_coding_role", "message": f"coding agent {label} has non-canonical role: {role}"})
            schema_ref = agent.get("schemaRef")
            if schema_ref and schema_ref not in schemas:
                issues.append({"code": "undefined_schema", "message": f"agent {label} references undefined schema: {schema_ref}"})
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
    if is_coding:
        verification_agents = [
            agent
            for phase in (plan.get("phases") or [])
            for agent in (phase.get("agents") or [])
            if str(agent.get("role") or "").strip().lower() == "verification"
        ]
        if not verification_agents:
            issues.append({"code": "missing_verification_role", "message": "coding workflow requires a verification agent"})
        else:
            required_verification_fields = {"verificationPassed", "checks", "blockingIssues"}
            has_strict_verification_schema = any(
                agent.get("strictSchema") is True
                and isinstance(schemas.get(agent.get("schemaRef")), dict)
                and required_verification_fields.issubset(set((schemas.get(agent.get("schemaRef")) or {}).get("required") or []))
                for agent in verification_agents
            )
            if not has_strict_verification_schema:
                issues.append({
                    "code": "missing_strict_verification_schema",
                    "message": "coding workflow verification agent must use a strict schema requiring verificationPassed, checks, and blockingIssues",
                })
        acceptance = plan.get("acceptance")
        acceptance_check_names = {
            str(check.get("type") if isinstance(check, dict) else check)
            for check in acceptance.get("checks", [])
        } if isinstance(acceptance, dict) and isinstance(acceptance.get("checks"), list) else set()
        if (
            not isinstance(acceptance, dict)
            or acceptance.get("required") is not True
            or not isinstance(acceptance.get("checks"), list)
            or not acceptance.get("checks")
        ):
            issues.append({"code": "missing_acceptance_contract", "message": "coding workflow requires a non-empty acceptance contract"})
        else:
            required_checks = {"python_unittest", "verification_schema"}
            if not required_checks.issubset(acceptance_check_names):
                issues.append({
                    "code": "incomplete_acceptance_contract",
                    "message": "coding workflow acceptance must require python_unittest and verification_schema",
                })
            invalid_checks = [
                str(check.get("type") if isinstance(check, dict) else check)
                for check in acceptance.get("checks")
                if str(check.get("type") if isinstance(check, dict) else check) not in {"python_unittest", "verification", "verification_schema"}
            ]
            if invalid_checks:
                issues.append({"code": "invalid_acceptance_check", "message": f"unsupported acceptance checks: {', '.join(invalid_checks)}"})
    return {"ok": not issues, "issues": issues}


def render_workflow_plan(plan: dict[str, Any]) -> str:
    lines: list[str] = []
    meta = plan.get("meta") or {}
    phases = [{"title": phase.get("title")} for phase in plan.get("phases") or []]
    lines.append("export const meta = " + json.dumps({"name": meta.get("name"), "description": meta.get("description"), "phases": phases}, ensure_ascii=False, indent=2))
    lines.append("")
    schemas = plan.get("schemas") or {}
    for name, schema in schemas.items():
        lines.append(f"const {name} = " + json.dumps(schema, ensure_ascii=False, indent=2))
        lines.append("")
    result_names: list[str] = []
    rendered_labels: dict[str, str] = {}
    for phase in plan.get("phases") or []:
        title = str(phase.get("title") or "")
        lines.append(f"phase('{_js_string(title)}')")
        agents = phase.get("agents") or []
        independent_agents = [agent for agent in agents if not (agent.get("dependsOn") or [])]
        if len(agents) > 1 and len(independent_agents) == len(agents):
            phase_vars = [_js_identifier(str(agent.get("label") or "agent")) for agent in agents]
            lines.append(f"const [{', '.join(phase_vars)}] = await parallel([")
            for agent in agents:
                label = str(agent.get("label") or "agent")
                prompt = str(agent.get("prompt") or "")
                options = {"label": label, "phase": title}
                if agent.get("role"):
                    options["role"] = str(agent["role"])
                if agent.get("retryPolicy"):
                    options["retryPolicy"] = agent["retryPolicy"]
                if agent.get("schemaRef"):
                    options["schema"] = {"__schema_ref__": agent["schemaRef"]}
                    if agent.get("strictSchema"):
                        options["strictSchema"] = True
                    else:
                        options["fallback"] = str(agent.get("fallback") or "text")
                lines.append(f"  () => agent(`{_template_string(prompt)}`, {_render_options(options)}),")
                rendered_labels[label] = _js_identifier(label)
            lines.append("])")
            result_names.extend(phase_vars)
            lines.append("")
            continue
        phase_vars: list[str] = []
        for agent in agents:
            label = str(agent.get("label") or "agent")
            var_name = _js_identifier(label)
            prompt = str(agent.get("prompt") or "")
            dependencies = [rendered_labels[item] for item in agent.get("dependsOn") or [] if item in rendered_labels]
            rendered_prompt = _template_string(prompt)
            if dependencies:
                rendered_prompt += "\n\n上游结果：${JSON.stringify({" + ", ".join(dependencies) + "})}"
            options = {"label": label, "phase": title}
            if agent.get("role"):
                options["role"] = str(agent["role"])
            if agent.get("retryPolicy"):
                options["retryPolicy"] = agent["retryPolicy"]
            if agent.get("schemaRef"):
                options["schema"] = {"__schema_ref__": agent["schemaRef"]}
                if agent.get("strictSchema"):
                    options["strictSchema"] = True
                else:
                    options["fallback"] = str(agent.get("fallback") or "text")
            lines.append(f"const {var_name} = await agent(`{rendered_prompt}`, {_render_options(options)})")
            phase_vars.append(var_name)
            rendered_labels[label] = var_name
        result_names.extend(phase_vars)
        lines.append("")
    acceptance = plan.get("acceptance") or {}
    acceptance_checks = acceptance.get("checks") if isinstance(acceptance, dict) else []
    if isinstance(acceptance_checks, list) and any(
        (check.get("type") if isinstance(check, dict) else check) == "python_unittest"
        for check in acceptance_checks
    ):
        lines.append("const __acceptanceGate = await runPythonUnittest(args.workspacePath, { pattern: 'test_*.py', phase: 'Verification', gateKey: 'workflow-acceptance' })")
        lines.append("")
    if result_names:
        lines.append("return { " + ", ".join(result_names) + " }")
    else:
        lines.append("return {}")
    return "\n".join(lines)


def _js_identifier(label: str) -> str:
    parts = re.sub(r"[^0-9A-Za-z_]+", "_", label).strip("_") or "agent"
    if parts[0].isdigit():
        parts = "agent_" + parts
    return parts


def _js_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _template_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace("`", "\\`").replace("${", "\\${")


def _render_options(options: dict[str, Any]) -> str:
    parts: list[str] = []
    for key, value in options.items():
        if isinstance(value, dict) and value.get("__schema_ref__"):
            parts.append(f"{key}: {value['__schema_ref__']}")
        elif isinstance(value, (dict, list)):
            parts.append(f"{key}: {json.dumps(value, ensure_ascii=False, separators=(',', ':'))}")
        elif isinstance(value, bool):
            parts.append(f"{key}: {'true' if value else 'false'}")
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            parts.append(f"{key}: {value}")
        else:
            parts.append(f"{key}: '{_js_string(str(value))}'")
    return "{ " + ", ".join(parts) + " }"
