import re
import tempfile
import unittest

from workflow_child_agent import FakeChildAgentRunner
from workflow_models import WorkflowRun
from workflow_planner import render_workflow_plan, validate_workflow_plan
from workflow_runtime import WorkflowRuntime
from workflow_scheduler import SchedulerConfig
from workflow_store import WorkflowStore


class WorkflowPlanValidatorTest(unittest.TestCase):
    def valid_plan(self):
        return {
            "taskType": "research",
            "meta": {"name": "valid", "description": "valid plan"},
            "phases": [
                {
                    "title": "Collect",
                    "agents": [
                        {
                            "label": "collector",
                            "prompt": "边界：不要读取 mykey.py、mykey.json、mcp.json；不要提交；收集资料。",
                            "schemaRef": "COLLECT_SCHEMA",
                            "dependsOn": [],
                        }
                    ],
                },
                {
                    "title": "Synthesize",
                    "agents": [
                        {
                            "label": "writer",
                            "prompt": "边界：不要读取 mykey.py、mykey.json、mcp.json；不要提交；综合上游结果。",
                            "dependsOn": ["collector"],
                        }
                    ],
                },
            ],
            "schemas": {"COLLECT_SCHEMA": {"type": "object", "required": ["sources"]}},
            "artifacts": ["sources", "synthesis"],
            "constraints": ["no_secret_files", "no_git_commit"],
        }

    def test_rejects_undefined_dependency_and_schema_without_prompt_boundary(self):
        plan = self.valid_plan()
        plan["phases"][0]["agents"][0]["schemaRef"] = "MISSING_SCHEMA"
        plan["phases"][0]["agents"][0]["prompt"] = "use process env"
        plan["phases"][1]["agents"][0]["dependsOn"] = ["missing-agent"]

        validation = validate_workflow_plan(plan)

        self.assertFalse(validation["ok"])
        self.assertEqual(
            {"undefined_schema", "undefined_dependency"},
            {issue["code"] for issue in validation["issues"]},
        )

    def test_rejects_same_phase_dependency_with_explicit_diagnostic(self):
        plan = self.valid_plan()
        plan["phases"] = [
            {
                "title": "Collect",
                "agents": [
                    {"label": "collector", "prompt": "collect", "dependsOn": []},
                    {"label": "writer", "prompt": "write", "dependsOn": ["collector"]},
                ],
            }
        ]

        validation = validate_workflow_plan(plan)

        self.assertFalse(validation["ok"])
        self.assertIn("same_phase_dependency", {issue["code"] for issue in validation["issues"]})

    def test_rejects_duplicate_dependency(self):
        plan = self.valid_plan()
        plan["phases"][1]["agents"][0]["dependsOn"] = ["collector", "collector"]

        validation = validate_workflow_plan(plan)

        self.assertFalse(validation["ok"])
        self.assertIn("duplicate_dependency", {issue["code"] for issue in validation["issues"]})

    def test_rejects_dependency_cycle_with_explicit_diagnostic(self):
        plan = self.valid_plan()
        plan["phases"] = [
            {"title": "First", "agents": [{"label": "first", "prompt": "first", "dependsOn": ["second"]}]},
            {"title": "Second", "agents": [{"label": "second", "prompt": "second", "dependsOn": ["first"]}]},
        ]

        validation = validate_workflow_plan(plan)

        self.assertFalse(validation["ok"])
        self.assertIn("dependency_cycle", {issue["code"] for issue in validation["issues"]})

    def test_accepts_agent_prompt_without_sensitive_file_template(self):
        plan = self.valid_plan()
        plan["phases"][0]["agents"][0]["prompt"] = "分析仓库中的普通 workflow 逻辑。"

        validation = validate_workflow_plan(plan)

        self.assertTrue(validation["ok"], validation)
        self.assertNotIn("missing_safety_boundary", {issue["code"] for issue in validation["issues"]})

    def test_allows_script_words_in_prompt_without_safety_boundary(self):
        plan = self.valid_plan()
        plan["phases"][0]["agents"][0]["prompt"] = "请 process 这段说明，并比较 import 与 fetch 的语义。"

        validation = validate_workflow_plan(plan)

        self.assertTrue(validation["ok"], validation)
        self.assertNotIn("forbidden_token", {issue["code"] for issue in validation["issues"]})

    def test_rejects_coding_plan_that_parallelizes_tests_and_implementation(self):
        plan = self.valid_plan()
        plan["taskType"] = "coding"
        plan["phases"] = [
            {
                "title": "Build",
                "agents": [
                    {
                        "label": "write-tests",
                        "prompt": "边界：不要读取 mykey.py；不要提交；先写 failing tests。",
                        "role": "tests",
                        "dependsOn": [],
                    },
                    {
                        "label": "implement-code",
                        "prompt": "边界：不要读取 mykey.py；不要提交；实现生产代码。",
                        "role": "implementation",
                        "dependsOn": [],
                    },
                ],
            }
        ]
        plan["schemas"] = {}

        validation = validate_workflow_plan(plan)

        self.assertFalse(validation["ok"])
        self.assertIn("coding_tests_parallel_implementation", {issue["code"] for issue in validation["issues"]})

    def test_rejects_coding_plan_without_acceptance_contract(self):
        plan = self.valid_plan()
        plan["taskType"] = "coding"
        plan["phases"] = [
            {
                "title": "Verification",
                "agents": [
                    {
                        "label": "verify",
                        "role": "verification",
                        "writeScope": ["src/"],
                        "prompt": "运行测试并输出结构化验收结果。",
                        "dependsOn": [],
                    }
                ],
            }
        ]
        plan["schemas"] = {}

        validation = validate_workflow_plan(plan)

        self.assertFalse(validation["ok"])
        self.assertIn("missing_verification_check", {issue["code"] for issue in validation["issues"]})

    def test_rejects_coding_plan_that_omits_roles_even_when_labels_describe_roles(self):
        plan = self.valid_plan()
        plan["taskType"] = "coding"
        plan["phases"] = [
            {
                "title": "Build",
                "agents": [
                    {
                        "label": "write-tests",
                        "writeScope": ["tests/"],
                        "prompt": "边界：不要读取 mykey.py；不要提交；先写 failing tests。",
                        "dependsOn": [],
                    },
                    {
                        "label": "implement-code",
                        "writeScope": ["src/"],
                        "prompt": "边界：不要读取 mykey.py；不要提交；实现生产代码。",
                        "dependsOn": [],
                    },
                ],
            }
        ]
        plan["schemas"] = {}

        validation = validate_workflow_plan(plan)

        self.assertFalse(validation["ok"])
        self.assertEqual(
            {"missing_coding_role", "missing_verification_check"},
            {issue["code"] for issue in validation["issues"]},
        )

    def test_rejects_noncanonical_coding_role_that_could_bypass_topology_check(self):
        plan = self.valid_plan()
        plan["taskType"] = "coding"
        plan["phases"] = [
            {
                "title": "Build",
                "agents": [
                    {
                        "label": "write-tests",
                        "role": "test-writer",
                        "prompt": "边界：不要读取 mykey.py；不要提交；先写 failing tests。",
                        "dependsOn": [],
                    },
                    {
                        "label": "implement-code",
                        "role": "implementation",
                        "prompt": "边界：不要读取 mykey.py；不要提交；实现生产代码。",
                        "dependsOn": [],
                    },
                ],
            }
        ]
        plan["schemas"] = {}

        validation = validate_workflow_plan(plan)

        self.assertFalse(validation["ok"])
        self.assertIn("invalid_coding_role", {issue["code"] for issue in validation["issues"]})

    def test_renderer_uses_parallel_for_independent_same_phase_agents(self):
        plan = self.valid_plan()
        plan["phases"][0]["agents"].append(
            {
                "label": "repo-scout",
                "prompt": "边界：不要读取 mykey.py、mykey.json、mcp.json；不要提交；检查仓库线索。",
                "dependsOn": [],
            }
        )

        validation = validate_workflow_plan(plan)
        script = render_workflow_plan(plan)

        self.assertTrue(validation["ok"], validation)
        self.assertIn("await parallel([", script)
        self.assertIn("label: 'collector'", script)
        self.assertIn("label: 'repo-scout'", script)
        self.assertIn("JSON.stringify", script)

    def test_renderer_propagates_agent_role_to_child_options(self):
        plan = self.valid_plan()
        plan["phases"][0]["agents"][0]["role"] = "verification"

        script = render_workflow_plan(plan)

        self.assertIn("role: 'verification'", script)

    def test_renderer_adds_host_test_gate_for_coding_acceptance_contract(self):
        plan = self.valid_plan()
        plan["taskType"] = "coding"
        plan["acceptance"] = {
            "required": True,
            "failWorkflowOnError": True,
            "checks": ["python_unittest", "verification_schema"],
        }
        plan["phases"][0]["agents"][0]["role"] = "implementation"
        plan["phases"][0]["agents"][0]["writeScope"] = ["src/"]

        script = render_workflow_plan(plan)

        self.assertIn("runPythonUnittest(args.workspacePath", script)

    def test_validator_applies_coding_contract_when_mixed_plan_declares_code_work(self):
        plan = self.valid_plan()
        plan["taskType"] = "mixed"
        plan["phases"] = [
            {"title": "Build", "agents": [
                {"label": "impl", "role": "implementation", "prompt": "write code", "dependsOn": []},
            ]},
        ]

        validation = validate_workflow_plan(plan)

        self.assertFalse(validation["ok"])
        codes = {issue["code"] for issue in validation["issues"]}
        self.assertIn("missing_verification_check", codes)

    def test_validator_ignores_role_optional_when_plan_declares_no_code_work(self):
        plan = self.valid_plan()
        plan["taskType"] = "research"
        plan["phases"] = [
            {"title": "Collect", "agents": [{"label": "collector", "prompt": "collect", "dependsOn": []}]},
            {"title": "Synthesize", "agents": [{"label": "writer", "prompt": "write", "dependsOn": ["collector"]}]},
        ]

        validation = validate_workflow_plan(plan)

        self.assertTrue(validation["ok"], validation)

    def test_write_plan_accepts_explicit_diff_check_without_verification_agent(self):
        plan = self.valid_plan()
        plan["taskType"] = "mixed"
        plan["phases"] = [
            {
                "title": "Build",
                "agents": [
                    {
                        "label": "impl",
                        "role": "implementation",
                        "writeScope": ["src/"],
                        "prompt": "write code",
                        "dependsOn": [],
                    }
                ],
            }
        ]
        plan["verification"] = {
            "level": "inline",
            "checks": [{"id": "diff", "kind": "diff", "required": True, "owner": "host"}],
        }

        validation = validate_workflow_plan(plan)

        self.assertTrue(validation["ok"], validation)

    def test_write_plan_without_required_check_is_rejected(self):
        plan = self.valid_plan()
        plan["taskType"] = "mixed"
        plan["phases"] = [
            {
                "title": "Build",
                "agents": [
                    {
                        "label": "impl",
                        "role": "implementation",
                        "writeScope": ["src/"],
                        "prompt": "write code",
                        "dependsOn": [],
                    }
                ],
            }
        ]
        plan["verification"] = {"level": "inline", "checks": []}

        validation = validate_workflow_plan(plan)

        self.assertFalse(validation["ok"])
        self.assertIn("missing_verification_check", {issue["code"] for issue in validation["issues"]})

    def test_renderer_skips_host_test_gate_for_non_coding_acceptance_contract(self):
        plan = self.valid_plan()
        plan["acceptance"] = {
            "required": True,
            "failWorkflowOnError": True,
            "checks": ["python_unittest", "verification_schema"],
        }

        script = render_workflow_plan(plan)

        self.assertNotIn("runPythonUnittest(args.workspacePath", script)

    def test_renderer_keeps_host_test_gate_for_mixed_plan_that_declares_code_work(self):
        plan = self.valid_plan()
        plan["taskType"] = "mixed"
        plan["acceptance"] = {
            "required": True,
            "failWorkflowOnError": True,
            "checks": ["python_unittest", "verification_schema"],
        }
        plan["phases"][0]["agents"][0]["role"] = "implementation"
        plan["phases"][0]["agents"][0]["writeScope"] = ["src/"]

        script = render_workflow_plan(plan)

        self.assertIn("runPythonUnittest(args.workspacePath", script)

    def test_renderer_keeps_cjk_labels_from_colliding_into_one_identifier(self):
        # A real deepseek run produced Chinese phase/agent labels; ASCII
        # sanitization mapped every one to "agent", generating
        # `const [agent, agent] = await parallel([...])` and killing the run
        # with "Identifier 'agent' has already been declared".
        plan = self.valid_plan()
        plan["taskType"] = "review"
        plan["phases"] = [
            {
                "title": "维度评审 fan out",
                "agents": [
                    {"label": "安全审查", "prompt": "a", "dependsOn": []},
                    {"label": "性能审查", "prompt": "b", "dependsOn": []},
                    {"label": "测试缺口审查", "prompt": "c", "dependsOn": []},
                ],
            },
            {"title": "验证", "agents": [{"label": "发现核验", "prompt": "d", "dependsOn": ["安全审查"]}]},
        ]

        script = render_workflow_plan(plan)

        declaration = script.split("= await parallel([", 1)[0]
        names = re.findall(r"const \[([^\]]+)\]", declaration)[0].split(", ")
        self.assertEqual(len(names), len(set(names)), f"duplicate identifiers in {declaration!r}")
        self.assertNotIn("[agent, agent]", script)

    def test_renderer_avoids_shadowing_runtime_helpers(self):
        plan = self.valid_plan()
        plan["phases"] = [
            {
                "title": "T",
                "agents": [
                    {"label": "agent", "prompt": "a", "dependsOn": []},
                    {"label": "phase", "prompt": "b", "dependsOn": []},
                ],
            }
        ]

        script = render_workflow_plan(plan)

        self.assertNotIn("const [agent,", script)
        self.assertNotIn("const phase =", script)

    def test_renderer_treats_prompt_template_expressions_as_literal_text(self):
        plan = self.valid_plan()
        literal_prompt = "边界：不要读取 mykey.py、mykey.json、mcp.json；不要提交；解释 ${log('INJECTED')} 和 ${1+2}，必须按字面量处理。"
        plan["phases"] = [
            {
                "title": "Literal Prompt",
                "agents": [
                    {
                        "label": "literal-check",
                        "prompt": literal_prompt,
                        "dependsOn": [],
                    }
                ],
            }
        ]
        plan["schemas"] = {}

        validation = validate_workflow_plan(plan)
        script = render_workflow_plan(plan)

        self.assertTrue(validation["ok"], validation)
        self.assertIn("\\${log('INJECTED')}", script)
        self.assertIn("\\${1+2}", script)

        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            run = store.create_run(WorkflowRun(run_id="wf_literal_prompt", session_id="session_literal", script=script, status="running"))
            outcome = WorkflowRuntime(
                store=store,
                runner=FakeChildAgentRunner(),
                scheduler_config=SchedulerConfig(max_concurrent=1, max_total=2),
                timeout_seconds=5.0,
            ).run(run)
            loaded = store.load_run(run.run_id)

        self.assertEqual("succeeded", outcome.run.status)
        self.assertEqual(["Literal Prompt"], outcome.phases)
        self.assertEqual([], outcome.logs)
        self.assertEqual(1, len(loaded.jobs))
        self.assertEqual(literal_prompt, loaded.jobs[0].prompt)

    def test_renderer_preserves_dependency_injection_while_escaping_prompt_literals(self):
        plan = self.valid_plan()
        downstream_prompt = "边界：不要读取 mykey.py、mykey.json、mcp.json；不要提交；汇总上游，保留 ${notInterpolation} 字面量。"
        plan["phases"] = [
            {
                "title": "Collect",
                "agents": [
                    {
                        "label": "collector",
                        "prompt": "边界：不要读取 mykey.py、mykey.json、mcp.json；不要提交；收集资料。",
                        "dependsOn": [],
                    }
                ],
            },
            {
                "title": "Synthesize",
                "agents": [
                    {
                        "label": "writer",
                        "prompt": downstream_prompt,
                        "dependsOn": ["collector"],
                    }
                ],
            },
        ]
        plan["schemas"] = {}

        validation = validate_workflow_plan(plan)
        script = render_workflow_plan(plan)

        self.assertTrue(validation["ok"], validation)
        self.assertIn("\\${notInterpolation}", script)
        self.assertIn("${JSON.stringify({collector})}", script)

        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            run = store.create_run(WorkflowRun(run_id="wf_dependency_prompt", session_id="session_dependency", script=script, status="running"))
            outcome = WorkflowRuntime(
                store=store,
                runner=FakeChildAgentRunner(),
                scheduler_config=SchedulerConfig(max_concurrent=1, max_total=3),
                timeout_seconds=5.0,
            ).run(run)
            loaded = store.load_run(run.run_id)

        self.assertEqual("succeeded", outcome.run.status)
        self.assertEqual(["Collect", "Synthesize"], outcome.phases)
        self.assertEqual([], outcome.logs)
        self.assertEqual(2, len(loaded.jobs))
        self.assertEqual("collector", loaded.jobs[0].metadata.get("label"))
        self.assertEqual("writer", loaded.jobs[1].metadata.get("label"))
        self.assertIn("${notInterpolation}", loaded.jobs[1].prompt)
        self.assertIn("上游结果：", loaded.jobs[1].prompt)
        self.assertIn("completed agent_1", loaded.jobs[1].prompt)


if __name__ == "__main__":
    unittest.main()
