import unittest

from workflow_planner import LLMWorkflowPlanner, WorkflowPlanner


class WorkflowPromptGuidanceTest(unittest.TestCase):
    def test_prompt_guides_execution_contract_and_runtime_boundaries(self):
        planner = LLMWorkflowPlanner(client=object(), fallback=WorkflowPlanner())
        prompt = planner._planner_prompt(
            "search several sources, write an html page, then verify it",
            {},
            issues=[],
            previous_plan=None,
        )
        for expected in (
            "executionContract",
            "不要只返回计划",
            "parallel",
            "pipeline",
            "schema",
            "budget",
            "terminal",
            "capability snapshot",
            "never write a concrete tool name",
            "toolProfile",
            "capabilities",
            "action id must match",
            "machine-checkable",
            "artifact_readback",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected.lower(), prompt.lower())


if __name__ == "__main__":
    unittest.main()
