from __future__ import annotations

import copy
import json
import os
import re
from dataclasses import dataclass, field
from typing import Any


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
        plan = self._build_plan(task_text, context, classification)
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
            boundary = _prompt_boundary(classification)
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
                                "prompt": f"{boundary}\n任务：{task_text}\n收集公开来源、关键 claims、风险和后续验证建议，返回结构化摘要。",
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
                                "prompt": f"{boundary}\n基于上游 Source Discovery 结果写中文综合报告，标注不确定性和建议。",
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
            boundary = _prompt_boundary(classification)
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
                                "prompt": f"{boundary}\n任务：{task_text}\n只读理解需求和相关文件，输出最小 TDD 切片建议；不要修改文件。",
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
                                "prompt": f"{boundary}\n基于理解结果先写或描述 failing tests，并明确如何看到红灯。",
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
                                "prompt": f"{boundary}\n在测试红灯后实现最小生产代码，使测试转绿。",
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
                                "prompt": f"{boundary}\n运行相关验证并总结风险、失败和后续建议。",
                                "dependsOn": ["implement"],
                            }
                        ],
                    },
                ],
                "schemas": {},
                "artifacts": ["understanding", "tests", "implementation", "verification"],
                "constraints": ["no_secret_files", "no_git_commit"],
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
                            "prompt": f"{_prompt_boundary(classification)}\n任务：{task_text}\n制定最小执行计划和验证建议。",
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
    def __init__(self, config_name: str = "native_oai_config"):
        self.config_name = str(config_name or "native_oai_config")
        self.raw_outputs: list[str] = []

    def complete(self, messages: list[dict]) -> dict:
        session = resolve_session(self.config_name)
        prompt = str((messages or [{}])[0].get("content") or "") + """

硬性输出要求：
- 只输出一个 JSON object。
- 不要 Markdown。
- 不要解释。
- 所有 agent.prompt 必须包含：不要读取 mykey.py、mykey.json、mcp.json；不要提交。
- 不要输出 JavaScript；只输出 WorkflowPlan JSON。
"""
        raw = "".join(str(chunk) for chunk in session.ask({"role": "user", "content": [{"type": "text", "text": prompt}]}))
        self.raw_outputs.append(raw)
        return parse_json_object(raw)


def parse_json_object(raw: str) -> dict:
    text = str(raw or "").strip()
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


def resolve_session(config_name: str):
    from llmcore import resolve_session as _resolve_session

    return _resolve_session(config_name)


def build_workflow_planner_from_env() -> WorkflowPlanner | LLMWorkflowPlanner:
    mode = str(os.environ.get("GA_WORKFLOW_PLANNER_MODE") or "deterministic").strip().lower()
    if mode not in {"prompt_guided", "llm", "real"}:
        return WorkflowPlanner()
    config_name = (
        os.environ.get("GA_WORKFLOW_PLANNER_CONFIG")
        or os.environ.get("GA_REAL_API_CONFIG")
        or "native_oai_config"
    )
    try:
        repair_attempts = int(os.environ.get("GA_WORKFLOW_PLANNER_REPAIR_ATTEMPTS") or "1")
    except ValueError:
        repair_attempts = 1
    return LLMWorkflowPlanner(
        client=NativeWorkflowPlannerClient(config_name=config_name),
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
            plan = self._request_plan(task_text, context, issues=[])
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
                plan = self._request_plan(task_text, context, issues=validation["issues"], previous_plan=plan)
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
                "如果任务要求审查安全/性能/测试缺口/回归风险，taskType 必须是 review，不要误判为 research。",
                "如果任务要求规划设计方案或实施计划且明确不要直接写代码，taskType 应为 planning 或 mixed。",
                "review 任务按 security/performance/test-gap/regression 等独立维度 fan out。",
                "coding 任务必须遵守 Understand -> Tests -> Implementation -> Verification，禁止 tests 与 implementation 并行。",
                "research 任务可多来源并行，synthesis 必须依赖上游结果，并应包含 credibility/evidence 检查。",
                "prompt 必须包含不要读取 mykey.py/mykey.json/mcp.json、不要提交等安全边界。",
                "phases 必须至少包含一个 phase；每个 phase 必须至少包含一个 agent。",
                "agent label 使用清晰英文短语，避免无意义缩写。",
            ],
            "requiredShape": {
                "taskType": "research | coding | review | debugging | planning | mixed",
                "meta": {"name": "...", "description": "..."},
                "phases": [{"title": "...", "agents": [{"label": "...", "prompt": "...", "dependsOn": []}]}],
                "schemas": {},
                "artifacts": [],
                "constraints": ["no_secret_files", "no_git_commit"],
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
    schemas = plan.get("schemas") or {}
    forbidden_tokens = ["require", "import", "process", "fs", "child_process", "fetch", "XMLHttpRequest", "Deno", "Bun", "WebSocket"]
    for phase_index, phase in enumerate(plan.get("phases") or []):
        phase_title = str(phase.get("title") or "")
        phase_labels: set[str] = set()
        phase_roles = {str(agent.get("role") or "") for agent in phase.get("agents") or []}
        if plan.get("taskType") == "coding" and "tests" in phase_roles and "implementation" in phase_roles:
            issues.append({"code": "coding_tests_parallel_implementation", "message": f"coding phase {phase_title} parallelizes tests and implementation"})
        for agent in phase.get("agents") or []:
            label = str(agent.get("label") or "")
            if not label:
                issues.append({"code": "missing_label", "message": f"phase {phase_title} has agent without label"})
                continue
            if label in labels or label in phase_labels:
                issues.append({"code": "duplicate_label", "message": f"duplicate agent label: {label}"})
            phase_labels.add(label)
            schema_ref = agent.get("schemaRef")
            if schema_ref and schema_ref not in schemas:
                issues.append({"code": "undefined_schema", "message": f"agent {label} references undefined schema: {schema_ref}"})
            for dependency in agent.get("dependsOn") or []:
                if dependency not in labels:
                    issues.append({"code": "undefined_dependency", "message": f"agent {label} depends on undefined or same-phase label: {dependency}"})
            prompt = str(agent.get("prompt") or "")
            for token in forbidden_tokens:
                if re.search(rf"\b{re.escape(token)}\b", prompt):
                    issues.append({"code": "forbidden_token", "message": f"agent {label} prompt contains forbidden token: {token}"})
            if "mykey.py" not in prompt or "不要提交" not in prompt:
                issues.append({"code": "missing_safety_boundary", "message": f"agent {label} prompt lacks required safety boundary"})
        labels.update(phase_labels)
    if not plan.get("phases"):
        issues.append({"code": "missing_phase", "message": "workflow plan requires at least one phase"})
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
                if agent.get("schemaRef"):
                    options["schema"] = {"__schema_ref__": agent["schemaRef"]}
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
            if dependencies:
                prompt = prompt + "\n\n上游结果：${JSON.stringify({" + ", ".join(dependencies) + "})}"
            options = {"label": label, "phase": title}
            if agent.get("schemaRef"):
                options["schema"] = {"__schema_ref__": agent["schemaRef"]}
            lines.append(f"const {var_name} = await agent(`{_template_string(prompt)}`, {_render_options(options)})")
            phase_vars.append(var_name)
            rendered_labels[label] = var_name
        result_names.extend(phase_vars)
        lines.append("")
    if result_names:
        lines.append("return { " + ", ".join(result_names) + " }")
    else:
        lines.append("return {}")
    return "\n".join(lines)


def _prompt_boundary(classification: dict[str, Any]) -> str:
    constraints = "；".join(str(item) for item in classification.get("constraints") or [])
    suffix = f" 额外约束：{constraints}" if constraints else ""
    return "边界：只做当前任务；不要读取 mykey.py、mykey.json、mcp.json 或任何凭据文件；不要提交；不要输出密钥；返回风险和建议。" + suffix


def _js_identifier(label: str) -> str:
    parts = re.sub(r"[^0-9A-Za-z_]+", "_", label).strip("_") or "agent"
    if parts[0].isdigit():
        parts = "agent_" + parts
    return parts


def _js_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace("'", "\\'")


def _template_string(value: str) -> str:
    return value.replace("\\", "\\\\").replace("`", "\\`")


def _render_options(options: dict[str, Any]) -> str:
    parts: list[str] = []
    for key, value in options.items():
        if isinstance(value, dict) and value.get("__schema_ref__"):
            parts.append(f"{key}: {value['__schema_ref__']}")
        else:
            parts.append(f"{key}: '{_js_string(str(value))}'")
    return "{ " + ", ".join(parts) + " }"
