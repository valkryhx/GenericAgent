import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from workflow_child_agent import FakeChildAgentRunner
from workflow_models import WorkflowRun
from workflow_planner import LLMWorkflowPlanner, NativeWorkflowPlannerClient, WorkflowPlanner, build_workflow_planner_from_env, parse_json_object, plan_produces_code
from workflow_runtime import WorkflowRuntime
from workflow_scheduler import SchedulerConfig
from workflow_store import WorkflowStore


class FakePlannerClient:
    def __init__(self, responses=None, error=None):
        self.responses = list(responses or [])
        self.error = error
        self.calls = []

    def complete(self, messages):
        self.calls.append(messages)
        if self.error:
            raise self.error
        if not self.responses:
            raise AssertionError("no fake planner response left")
        return self.responses.pop(0)


def review_plan():
    boundary = "边界：不要读取 mykey.py、mykey.json、mcp.json；不要提交。"
    return {
        "taskType": "review",
        "meta": {"name": "dynamic-review", "description": "Review PR across risk dimensions"},
        "phases": [
            {
                "title": "Review",
                "agents": [
                    {"label": "security-review", "prompt": f"{boundary} 从安全角度审查。", "dependsOn": []},
                    {"label": "performance-review", "prompt": f"{boundary} 从性能角度审查。", "dependsOn": []},
                    {"label": "test-gap-review", "prompt": f"{boundary} 从测试缺口角度审查。", "dependsOn": []},
                ],
            },
            {
                "title": "Verify",
                "agents": [
                    {"label": "verify-findings", "prompt": f"{boundary} 反驳并验证上游 findings。", "dependsOn": ["security-review", "performance-review", "test-gap-review"]},
                ],
            },
            {
                "title": "Synthesis",
                "agents": [
                    {"label": "review-report", "prompt": f"{boundary} 汇总 verified findings。", "dependsOn": ["verify-findings"]},
                ],
            },
        ],
        "schemas": {},
        "artifacts": ["findings", "verified", "report"],
        "constraints": ["no_secret_files", "no_git_commit"],
    }


def invalid_coding_parallel_plan():
    boundary = "边界：不要读取 mykey.py、mykey.json、mcp.json；不要提交。"
    return {
        "taskType": "coding",
        "meta": {"name": "bad-coding", "description": "Bad coding plan"},
        "phases": [
            {
                "title": "Build",
                "agents": [
                    {"label": "write-failing-tests", "role": "tests", "prompt": f"{boundary} 先写红灯测试。", "dependsOn": []},
                    {"label": "implement-minimal-code", "role": "implementation", "prompt": f"{boundary} 实现最小代码。", "dependsOn": []},
                ],
            }
        ],
        "schemas": {},
        "artifacts": [],
        "constraints": ["no_secret_files", "no_git_commit"],
    }


def omitted_role_coding_parallel_plan():
    boundary = "边界：不要读取 mykey.py、mykey.json、mcp.json；不要提交。"
    return {
        "taskType": "coding",
        "meta": {"name": "missing-role-coding", "description": "Coding plan without agent roles"},
        "phases": [
            {
                "title": "Build",
                "agents": [
                    {"label": "write-failing-tests", "prompt": f"{boundary} 先写红灯测试。", "dependsOn": []},
                    {"label": "implement-minimal-code", "prompt": f"{boundary} 实现最小代码。", "dependsOn": []},
                ],
            }
        ],
        "schemas": {},
        "artifacts": [],
        "constraints": ["no_secret_files", "no_git_commit"],
    }


def repaired_coding_plan():
    boundary = "边界：不要读取 mykey.py、mykey.json、mcp.json；不要提交。"
    return {
        "taskType": "coding",
        "meta": {"name": "repaired-coding", "description": "TDD ordered coding plan"},
        "phases": [
            {"title": "Understand", "agents": [{"label": "understand", "role": "understanding", "prompt": f"{boundary} 理解任务。", "dependsOn": []}]},
            {"title": "Tests", "agents": [{"label": "write-failing-tests", "role": "tests", "prompt": f"{boundary} 先写 failing tests 并确认红灯。", "dependsOn": ["understand"]}]},
            {"title": "Implementation", "agents": [{"label": "implement-minimal-code", "role": "implementation", "prompt": f"{boundary} 红灯后实现最小代码。", "dependsOn": ["write-failing-tests"]}]},
            {"title": "Verification", "agents": [{"label": "run-tests", "role": "verification", "prompt": f"{boundary} 运行相关测试并返回 verificationPassed、checks、blockingIssues。", "schemaRef": "VERIFICATION_SCHEMA", "strictSchema": True, "dependsOn": ["implement-minimal-code"]}]},
        ],
        "schemas": {
            "VERIFICATION_SCHEMA": {
                "type": "object",
                "required": ["verificationPassed", "checks", "blockingIssues"],
            }
        },
        "artifacts": ["tests", "implementation", "verification"],
        "constraints": ["no_secret_files", "no_git_commit"],
        "acceptance": {
            "required": True,
            "failWorkflowOnError": True,
            "checks": ["python_unittest", "verification_schema"],
        },
    }
def planning_plan():
    boundary = "边界：不要读取 mykey.py、mykey.json、mcp.json；不要提交。"
    return {
        "taskType": "planning",
        "meta": {"name": "dynamic-planning", "description": "Plan cross-system workflow feature"},
        "phases": [
            {"title": "Context Discovery", "agents": [{"label": "context-discovery", "prompt": f"{boundary} 梳理前后端和运行时上下文。", "dependsOn": []}]},
            {"title": "Design Alternatives", "agents": [{"label": "design-alternatives", "prompt": f"{boundary} 提出 2-3 个设计方案。", "dependsOn": ["context-discovery"]}]},
            {"title": "Risk Review", "agents": [{"label": "risk-review", "prompt": f"{boundary} 审查风险和边界。", "dependsOn": ["design-alternatives"]}]},
            {"title": "Implementation Plan", "agents": [{"label": "implementation-plan", "prompt": f"{boundary} 只输出实施计划，不直接写代码。", "dependsOn": ["risk-review"]}]},
        ],
        "schemas": {},
        "artifacts": ["context", "alternatives", "risks", "plan"],
        "constraints": ["no_secret_files", "no_git_commit"],
    }


def research_credibility_plan():
    boundary = "边界：不要读取 mykey.py、mykey.json、mcp.json；不要提交。"
    return {
        "taskType": "research",
        "meta": {"name": "dynamic-research", "description": "Research with source discovery, credibility check, and synthesis"},
        "phases": [
            {
                "title": "Source Discovery",
                "agents": [
                    {"label": "web-source-discovery", "prompt": f"{boundary} 搜索公开来源。", "dependsOn": []},
                    {"label": "repo-source-discovery", "prompt": f"{boundary} 检查仓库内证据。", "dependsOn": []},
                ],
            },
            {"title": "Credibility Check", "agents": [{"label": "credibility-check", "prompt": f"{boundary} 评估来源可信度和矛盾点。", "dependsOn": ["web-source-discovery", "repo-source-discovery"]}]},
            {"title": "Synthesis", "agents": [{"label": "research-synthesis", "prompt": f"{boundary} 基于可信度检查写综合结论。", "dependsOn": ["credibility-check"]}]},
        ],
        "schemas": {},
        "artifacts": ["sources", "credibility", "synthesis"],
        "constraints": ["no_secret_files", "no_git_commit"],
    }


def interpolation_literal_plan():
    boundary = "边界：不要读取 mykey.py、mykey.json、mcp.json；不要提交。"
    return {
        "taskType": "review",
        "meta": {"name": "interpolation-literal", "description": "Prompt literal interpolation regression"},
        "phases": [
            {
                "title": "Review",
                "agents": [
                    {
                        "label": "literal-review",
                        "prompt": f"{boundary} 审查代码片段 `${{log('INJECTED')}}` 和 `${{1+2}}`，必须按字面量处理。",
                        "dependsOn": [],
                    }
                ],
            }
        ],
        "schemas": {},
        "artifacts": ["review"],
        "constraints": ["no_secret_files", "no_git_commit"],
    }


def verification_synthesis_same_phase_plan():
    boundary = "边界：不要读取 mykey.py、mykey.json、mcp.json；不要提交。"
    return {
        "taskType": "mixed",
        "meta": {"name": "same-phase-verification-synthesis", "description": "Verification and synthesis crammed into one phase"},
        "phases": [
            {"title": "Research and Temporary Coding", "agents": [{"label": "mcp-research-agent", "role": "understanding", "prompt": f"{boundary} 调用真实 MCP 搜索。", "dependsOn": []}]},
            {"title": "Temporary Workspace Coding", "agents": [{"label": "superpowers-coding-agent", "role": "implementation", "prompt": f"{boundary} 加载 using-superpowers 后在临时 workspace 编码。", "dependsOn": ["mcp-research-agent"]}]},
            {
                "title": "Verification and Synthesis",
                "agents": [
                    {"label": "verification-agent", "role": "verification", "prompt": f"{boundary} 校验编码产物。", "dependsOn": ["superpowers-coding-agent"]},
                    {"label": "synthesis-agent", "role": "synthesis", "prompt": f"{boundary} 汇总裁决。", "dependsOn": ["verification-agent"]},
                ],
            },
        ],
        "schemas": {},
        "artifacts": ["research", "coding", "synthesis"],
        "constraints": ["no_secret_files", "no_git_commit"],
        "acceptance": {"required": True, "failWorkflowOnError": True, "checks": ["python_unittest", "verification_schema"]},
    }


class LLMWorkflowPlannerTest(unittest.TestCase):
    def test_prompt_guided_planner_splits_same_phase_dependency_into_later_phase(self):
        client = FakePlannerClient(responses=[verification_synthesis_same_phase_plan()])
        planner = LLMWorkflowPlanner(client=client, max_repair_attempts=1)

        draft = planner.plan(
            "设计一个复杂 GA workflow 真实 E2E：MCP research + using-superpowers coding + synthesis",
            context={"constraints": ["不要读取 mykey.py", "不要提交"]},
        )

        self.assertTrue(draft.validation["ok"], draft.validation)
        self.assertEqual(1, len(client.calls))
        titles = [phase["title"] for phase in draft.plan["phases"]]
        self.assertEqual(len(titles), len(set(titles)))
        phase_of = {}
        for index, phase in enumerate(draft.plan["phases"]):
            for agent in phase["agents"]:
                phase_of[agent["label"]] = index
        self.assertLess(phase_of["superpowers-coding-agent"], phase_of["verification-agent"])
        self.assertLess(phase_of["verification-agent"], phase_of["synthesis-agent"])

    def test_prompt_guided_planner_uses_llm_plan_json_for_dynamic_review_topology(self):
        client = FakePlannerClient(responses=[review_plan()])
        planner = LLMWorkflowPlanner(client=client)

        draft = planner.plan(
            "全面审查这个 PR 的安全、性能、测试缺口和回归风险",
            context={"constraints": ["不要读取 mykey.py", "不要提交"]},
        )

        self.assertEqual("review", draft.classification["taskType"])
        self.assertTrue(draft.validation["ok"], draft.validation)
        self.assertEqual(["Review", "Verify", "Synthesis"], [phase["title"] for phase in draft.plan["phases"]])
        self.assertIn("await parallel([", draft.script)
        self.assertIn("label: 'security-review'", draft.script)
        self.assertIn("label: 'performance-review'", draft.script)
        self.assertIn("label: 'test-gap-review'", draft.script)
        self.assertIn("JSON.stringify", draft.script)
        self.assertEqual(1, len(client.calls))
        self.assertIn("WorkflowPlan JSON", client.calls[0][0]["content"])
        self.assertIn("不要输出 JS", client.calls[0][0]["content"])
        self.assertIn('"classificationHint": "review"', client.calls[0][0]["content"])
        self.assertIn("phases must be a non-empty array", client.calls[0][0]["content"])

    def test_prompt_guided_request_does_not_require_sensitive_prompt_boundary(self):
        client = FakePlannerClient(responses=[review_plan()])
        planner = LLMWorkflowPlanner(client=client)

        draft = planner.plan("审查普通 workflow 任务", context={"constraints": ["只读"]})

        self.assertTrue(draft.validation["ok"], draft.validation)
        request_text = client.calls[0][0]["content"]
        self.assertNotIn("mykey.py", request_text)
        self.assertNotIn("mykey.json", request_text)
        self.assertNotIn("mcp.json", request_text)
        self.assertNotIn("所有 agent.prompt 必须包含", request_text)

    def test_prompt_guided_planner_repairs_invalid_coding_parallel_plan(self):
        client = FakePlannerClient(responses=[invalid_coding_parallel_plan(), repaired_coding_plan()])
        planner = LLMWorkflowPlanner(client=client, max_repair_attempts=1)

        draft = planner.plan(
            "实现 workflow controller 的 planned run 入口",
            context={"constraints": ["不要读取 mykey.py", "不要提交"]},
        )

        self.assertTrue(draft.validation["ok"], draft.validation)
        self.assertEqual(["Understand", "Tests", "Implementation", "Verification"], [phase["title"] for phase in draft.plan["phases"]])
        self.assertLess(draft.script.index("label: 'write-failing-tests'"), draft.script.index("label: 'implement-minimal-code'"))
        self.assertNotIn("coding_tests_parallel_implementation", {issue["code"] for issue in draft.validation["issues"]})
        self.assertEqual(2, len(client.calls))
        self.assertIn("coding_tests_parallel_implementation", client.calls[1][0]["content"])

    def test_task_type_alone_does_not_trigger_coding_role_repair(self):
        client = FakePlannerClient(responses=[omitted_role_coding_parallel_plan()])
        planner = LLMWorkflowPlanner(client=client, max_repair_attempts=1)

        draft = planner.plan("technical task", context={})

        self.assertTrue(draft.validation["ok"], draft.validation)
        self.assertEqual("missing-role-coding", draft.plan["meta"]["name"])
        self.assertEqual(1, len(client.calls))

    def test_prompt_guided_planner_normalizes_missing_verification_schema(self):
        plan = repaired_coding_plan()
        plan["schemas"] = {}
        verification = plan["phases"][-1]["agents"][0]
        verification.pop("schemaRef", None)
        verification.pop("strictSchema", None)
        client = FakePlannerClient(responses=[plan])
        planner = LLMWorkflowPlanner(client=client, max_repair_attempts=0)

        draft = planner.plan("实现一个必须 TDD 的解析器", context={"constraints": ["不要提交"]})

        self.assertTrue(draft.validation["ok"], draft.validation)
        normalized = draft.plan["phases"][-1]["agents"][0]
        self.assertNotIn("strictSchema", normalized)
        self.assertNotIn("schemaRef", normalized)
        self.assertNotIn("GA_WORKFLOW_VERIFICATION_SCHEMA", draft.plan["schemas"])

    def test_task_type_is_not_a_hard_coding_signal(self):
        plan = {"taskType": "coding", "phases": [{"title": "Research", "agents": [{"label": "inspect", "role": "understanding"}]}]}
        self.assertFalse(plan_produces_code(plan))

        client = FakePlannerClient(responses=[plan])
        draft = LLMWorkflowPlanner(client=client, max_repair_attempts=0).plan("read only research", context={})
        issue_codes = {issue["code"] for issue in draft.validation["issues"]}
        self.assertNotIn("missing_verification_check", issue_codes)
        self.assertNotIn("missing_coding_role", issue_codes)

    def test_planner_prompt_treats_task_type_as_hint_and_requires_explicit_checks(self):
        client = FakePlannerClient(responses=[research_credibility_plan()])
        planner = LLMWorkflowPlanner(client=client, max_repair_attempts=0)
        planner.plan("technical research", context={})
        prompt = client.calls[0][0]["content"]
        self.assertIn("taskType", prompt)
        self.assertIn("verification.checks", prompt)
        self.assertNotIn("\"checks\": [\"verification_schema\"]", prompt)

    def test_prompt_guided_planner_normalizes_ultracode_execution_contract(self):
        plan = review_plan()
        client = FakePlannerClient(responses=[plan])
        planner = LLMWorkflowPlanner(client=client, max_repair_attempts=0)

        draft = planner.plan("审查一个跨模块 workflow 变更", context={"constraints": ["不要提交"]})

        self.assertTrue(draft.validation["ok"], draft.validation)
        self.assertEqual("workflow", draft.plan["mode"])
        self.assertIn(draft.plan["riskLevel"], {"low", "medium", "high"})
        self.assertTrue(draft.plan["successCriteria"])
        # The review plan has real cross-packet consumption edges
        # (verify-findings depends on the three reviews, review-report depends
        # on verify-findings). ultracode treats "one packet produces a surface
        # another packet consumes" as full regardless of the taskType label.
        self.assertEqual("full", draft.plan["evalContract"]["level"])
        self.assertEqual(
            {"security-review", "performance-review", "test-gap-review", "verify-findings"},
            {item["producer"] for item in draft.plan["evalContract"]["sharedSurfaces"]},
        )
        self.assertIn("requiredChecks", draft.plan["evalContract"])
        self.assertEqual(len(draft.plan["phases"]), draft.plan["orchestration"]["maxWaves"])
        self.assertEqual(5, draft.plan["orchestration"]["maxAgents"])

    def test_prompt_guided_planner_falls_back_to_deterministic_planner_when_client_fails(self):
        client = FakePlannerClient(error=RuntimeError("planner provider down"))
        planner = LLMWorkflowPlanner(client=client)

        draft = planner.plan(
            "调研 Claude Code dynamic workflow",
            context={"constraints": ["不要读取 mykey.py", "不要提交"]},
        )

        self.assertEqual("research", draft.classification["taskType"])
        self.assertEqual("fallback_deterministic", draft.validation["mode"])
        self.assertTrue(draft.validation["ok"], draft.validation)
        self.assertEqual("dynamic-workflow-research", draft.plan["meta"]["name"])
        self.assertEqual(1, len(client.calls))

    def test_prompt_guided_planner_uses_llm_planning_topology_without_writing_code(self):
        client = FakePlannerClient(responses=[planning_plan()])
        planner = LLMWorkflowPlanner(client=client)

        draft = planner.plan(
            "规划一个跨前端、后端和 workflow runtime 的 prompt guided planner feature",
            context={"constraints": ["不要读取 mykey.py", "不要提交"]},
        )

        self.assertEqual("planning", draft.classification["taskType"])
        self.assertTrue(draft.validation["ok"], draft.validation)
        self.assertEqual(
            ["Context Discovery", "Design Alternatives", "Risk Review", "Implementation Plan"],
            [phase["title"] for phase in draft.plan["phases"]],
        )
        self.assertNotIn("role: 'implementation'", draft.script)
        self.assertIn("不直接写代码", draft.script)

    def test_prompt_guided_planner_script_runs_with_fake_runtime(self):
        client = FakePlannerClient(responses=[review_plan()])
        planner = LLMWorkflowPlanner(client=client)
        draft = planner.plan("审查 PR 风险", context={"constraints": ["不要读取 mykey.py", "不要提交"]})

        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            run = store.create_run(WorkflowRun(run_id="wf_prompt_guided", session_id="session_prompt", script=draft.script, status="running"))
            outcome = WorkflowRuntime(
                store=store,
                runner=FakeChildAgentRunner(),
                scheduler_config=SchedulerConfig(max_concurrent=3, max_total=5),
                timeout_seconds=5.0,
            ).run(run)

            self.assertEqual("succeeded", outcome.run.status)
            self.assertEqual(["Review", "Verify", "Synthesis"], outcome.phases)
            loaded = store.load_run(run.run_id)
            self.assertEqual(["security-review", "performance-review", "test-gap-review", "verify-findings", "review-report"], [job.metadata.get("label") for job in loaded.jobs])

    def test_prompt_guided_planner_preserves_template_expressions_in_agent_prompt(self):
        client = FakePlannerClient(responses=[interpolation_literal_plan()])
        planner = LLMWorkflowPlanner(client=client)
        draft = planner.plan("审查包含 JS template literal 的代码", context={"constraints": ["不要读取 mykey.py", "不要提交"]})

        self.assertTrue(draft.validation["ok"], draft.validation)
        self.assertIn("\\${log('INJECTED')}", draft.script)
        self.assertIn("\\${1+2}", draft.script)

        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            run = store.create_run(WorkflowRun(run_id="wf_prompt_guided_literal", session_id="session_prompt_literal", script=draft.script, status="running"))
            outcome = WorkflowRuntime(
                store=store,
                runner=FakeChildAgentRunner(),
                scheduler_config=SchedulerConfig(max_concurrent=1, max_total=2),
                timeout_seconds=5.0,
            ).run(run)
            loaded = store.load_run(run.run_id)

        self.assertEqual("succeeded", outcome.run.status)
        self.assertEqual(["Review"], outcome.phases)
        self.assertEqual([], outcome.logs)
        self.assertEqual(1, len(loaded.jobs))
        self.assertIn("${log('INJECTED')}", loaded.jobs[0].prompt)
        self.assertIn("${1+2}", loaded.jobs[0].prompt)

    def test_prompt_guided_planner_uses_research_credibility_topology(self):
        client = FakePlannerClient(responses=[research_credibility_plan()])
        planner = LLMWorkflowPlanner(client=client)

        draft = planner.plan("调研某个技术方案的可信度和风险", context={"constraints": ["不要读取 mykey.py", "不要提交"]})

        self.assertEqual("research", draft.classification["taskType"])
        self.assertTrue(draft.validation["ok"], draft.validation)
        self.assertEqual(["Source Discovery", "Credibility Check", "Synthesis"], [phase["title"] for phase in draft.plan["phases"]])
        self.assertIn("await parallel([", draft.script)
        self.assertIn("label: 'credibility-check'", draft.script)
        self.assertIn("label: 'research-synthesis'", draft.script)
        self.assertIn("JSON.stringify", draft.script)

    def test_prompt_guided_planner_returns_rejected_draft_when_repair_attempts_exhausted(self):
        client = FakePlannerClient(responses=[invalid_coding_parallel_plan()])
        planner = LLMWorkflowPlanner(client=client, max_repair_attempts=0)

        draft = planner.plan("实现一个必须 TDD 的功能", context={"constraints": ["不要读取 mykey.py", "不要提交"]})

        self.assertEqual("rejected", draft.validation["mode"])
        self.assertFalse(draft.validation["ok"])
        self.assertEqual("coding", draft.classification["taskType"])
        self.assertEqual("", draft.script)
        self.assertIn("coding_tests_parallel_implementation", {issue["code"] for issue in draft.validation["issues"]})
        self.assertEqual("bad-coding", draft.plan["meta"]["name"])
        self.assertEqual("prompt_guided_rejected", draft.context["plannerMode"])

    def test_prompt_guided_rejected_draft_persists_validation_evidence(self):
        client = FakePlannerClient(responses=[invalid_coding_parallel_plan()])
        planner = LLMWorkflowPlanner(client=client, max_repair_attempts=0)
        draft = planner.plan("实现一个必须 TDD 的功能", context={"constraints": ["不要读取 mykey.py", "不要提交"]})

        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            run = store.create_run(WorkflowRun(run_id="wf_rejected_prompt", session_id="session_prompt", script=draft.script))
            draft_ref = store.write_workflow_draft(run, draft)

            data = json.loads((Path(run.artifact_dir) / draft_ref).read_text(encoding="utf-8"))
            self.assertEqual("rejected", data["validation"]["mode"])
            self.assertEqual("prompt_guided_rejected", data["context"]["plannerMode"])
            self.assertEqual("bad-coding", data["plan"]["meta"]["name"])
            self.assertIn("coding_tests_parallel_implementation", {issue["code"] for issue in data["validation"]["issues"]})

class NativeWorkflowPlannerClientTest(unittest.TestCase):
    def test_parse_json_object_tolerates_extra_json_after_first_object(self):
        raw = '{"taskType":"review","phases":[]}\n{"ignored":true}'

        parsed = parse_json_object(raw)

        self.assertEqual("review", parsed["taskType"])
        self.assertEqual([], parsed["phases"])

    def test_native_client_uses_yaml_session_and_parses_json_without_markdown(self):
        class FakeSession:
            def __init__(self):
                self.messages = []

            def ask(self, message):
                self.messages.append(message)
                return ['```json\n', json.dumps(review_plan(), ensure_ascii=False), '\n```']

        fake_session = FakeSession()
        with patch("workflow_llm.make_session", return_value=fake_session) as make_sess:
            client = NativeWorkflowPlannerClient(profile_name="grok")
            response = client.complete([{"role": "system", "content": "planner prompt"}])

        self.assertEqual(review_plan()["taskType"], response["taskType"])
        make_sess.assert_called_once()
        self.assertEqual("grok", client.profile_name)
        self.assertEqual("user", fake_session.messages[0]["role"])
        text = fake_session.messages[0]["content"][0]["text"]
        self.assertIn("planner prompt", text)
        self.assertIn("只输出一个 JSON object", text)
        self.assertNotIn("mykey.py", text)
        self.assertNotIn("mykey.json", text)
        self.assertNotIn("mcp.json", text)
        self.assertIn("不要输出 JavaScript", text)

    def test_build_workflow_planner_from_env_defaults_to_deterministic(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GA_WORKFLOW_PLANNER_MODE", None)
            planner = build_workflow_planner_from_env()

        self.assertIsInstance(planner, WorkflowPlanner)

    def test_build_workflow_planner_from_env_returns_prompt_guided_planner(self):
        with patch.dict(
            os.environ,
            {
                "GA_WORKFLOW_PLANNER_MODE": "prompt_guided",
                "GA_WORKFLOW_LLM_PROFILE": "grok",
                "GA_WORKFLOW_PLANNER_REPAIR_ATTEMPTS": "1",
            },
            clear=False,
        ):
            planner = build_workflow_planner_from_env()

        self.assertIsInstance(planner, LLMWorkflowPlanner)
        self.assertIsInstance(planner.client, NativeWorkflowPlannerClient)
        self.assertEqual("grok", planner.client.profile_name or planner.client.config_name)
        self.assertEqual(1, planner.max_repair_attempts)

    def test_build_workflow_planner_from_env_accepts_real_api_alias(self):
        with patch.dict(
            os.environ,
            {"GA_WORKFLOW_PLANNER_MODE": "real", "GA_WORKFLOW_LLM_PROFILE": "grok"},
            clear=False,
        ):
            planner = build_workflow_planner_from_env()

        self.assertIsInstance(planner, LLMWorkflowPlanner)
        self.assertEqual("grok", planner.client.profile_name or planner.client.config_name)

    def test_build_workflow_planner_from_env_ignores_legacy_mykey_config(self):
        with patch.dict(
            os.environ,
            {"GA_WORKFLOW_PLANNER_MODE": "real", "GA_REAL_API_CONFIG": "native_oai_config"},
            clear=False,
        ):
            os.environ.pop("GA_WORKFLOW_LLM_PROFILE", None)
            planner = build_workflow_planner_from_env()

        self.assertIsInstance(planner, LLMWorkflowPlanner)
        # legacy mykey key dropped → client has empty profile (uses binding_from_env at complete-time)
        self.assertFalse(str(planner.client.profile_name or "").startswith("native_"))


if __name__ == "__main__":
    unittest.main()
    def test_prompt_guided_planner_does_not_split_cyclic_same_phase_dependency(self):
        plan = verification_synthesis_same_phase_plan()
        plan["phases"][2]["agents"][1]["dependsOn"] = ["verification-agent"]
        plan["phases"][2]["agents"][0]["dependsOn"] = ["superpowers-coding-agent", "synthesis-agent"]
        client = FakePlannerClient(responses=[plan, plan])
        planner = LLMWorkflowPlanner(client=client, max_repair_attempts=1)

        draft = planner.plan(
            "设计一个复杂 GA workflow 真实 E2E：MCP research + using-superpowers coding + synthesis",
            context={"constraints": ["不要读取 mykey.py", "不要提交"]},
        )

        self.assertFalse(draft.validation["ok"])
        self.assertEqual("rejected", draft.validation["mode"])
        self.assertEqual(2, len(client.calls))
