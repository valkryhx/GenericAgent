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

    def test_rejects_coding_plan_that_omits_roles_even_when_labels_describe_roles(self):
        plan = self.valid_plan()
        plan["taskType"] = "coding"
        plan["phases"] = [
            {
                "title": "Build",
                "agents": [
                    {
                        "label": "write-tests",
                        "prompt": "边界：不要读取 mykey.py；不要提交；先写 failing tests。",
                        "dependsOn": [],
                    },
                    {
                        "label": "implement-code",
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
            {"missing_coding_role"},
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
