import tempfile
import unittest
from pathlib import Path

from tests.prompt_guided_planner_real_e2e import run_fake_runtime
from workflow_planner import render_workflow_plan


class PromptGuidedPlannerRealE2EContractTest(unittest.TestCase):
    def test_fake_runtime_accepts_valid_large_planner_output(self):
        boundary = "边界：不要读取 mykey.py、mykey.json、mcp.json；不要提交。"
        agents = [
            {
                "label": f"planning-agent-{index}",
                "prompt": f"{boundary} 规划第 {index} 个独立检查项。",
                "dependsOn": [],
            }
            for index in range(13)
        ]
        plan = {
            "taskType": "planning",
            "meta": {"name": "large-planning", "description": "Valid high fan-out planning workflow"},
            "phases": [{"title": "Planning Fanout", "agents": agents}],
            "schemas": {},
            "artifacts": ["plan"],
            "constraints": ["no_secret_files", "no_git_commit"],
        }

        with tempfile.TemporaryDirectory() as tmp:
            runtime = run_fake_runtime(Path(tmp), "large_planning", render_workflow_plan(plan))

        self.assertEqual("succeeded", runtime["status"])
        self.assertEqual(13, len(runtime["jobLabels"]))
        self.assertTrue(all(status == "succeeded" for status in runtime["jobStatuses"]))


if __name__ == "__main__":
    unittest.main()
