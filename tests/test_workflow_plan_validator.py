import unittest

from workflow_planner import render_workflow_plan, validate_workflow_plan


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

    def test_rejects_undefined_dependency_schema_forbidden_token_and_missing_boundary(self):
        plan = self.valid_plan()
        plan["phases"][0]["agents"][0]["schemaRef"] = "MISSING_SCHEMA"
        plan["phases"][0]["agents"][0]["prompt"] = "use process env"
        plan["phases"][1]["agents"][0]["dependsOn"] = ["missing-agent"]

        validation = validate_workflow_plan(plan)

        self.assertFalse(validation["ok"])
        self.assertEqual(
            {"undefined_schema", "forbidden_token", "missing_safety_boundary", "undefined_dependency"},
            {issue["code"] for issue in validation["issues"]},
        )

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


if __name__ == "__main__":
    unittest.main()
