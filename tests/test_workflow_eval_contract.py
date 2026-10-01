import unittest

from workflow_planner import (
    _normalize_plan_contract,
    _normalize_workflow_execution_contract,
    plan_eval_level,
    plan_risk_level,
    plan_shared_surfaces,
)


def parallel_plan():
    return {
        "taskType": "research",
        "meta": {"name": "parallel", "description": "parallel packets, no cross consumption"},
        "phases": [
            {
                "title": "Collect",
                "agents": [
                    {"label": "a", "prompt": "collect a", "dependsOn": []},
                    {"label": "b", "prompt": "collect b", "dependsOn": []},
                ],
            }
        ],
    }


def consuming_plan():
    return {
        "taskType": "research",
        "meta": {"name": "consuming", "description": "one packet consumes another"},
        "phases": [
            {"title": "Collect", "agents": [{"label": "a", "prompt": "collect", "dependsOn": []}]},
            {"title": "Synthesize", "agents": [{"label": "s", "prompt": "synthesize", "dependsOn": ["a"]}]},
        ],
    }


class EvalContractAxisTest(unittest.TestCase):
    def test_shared_surfaces_point_at_consumer_edges(self):
        surfaces = plan_shared_surfaces(consuming_plan())
        self.assertEqual(1, len(surfaces))
        surface = surfaces[0]
        self.assertEqual("a", surface["producer"])
        self.assertEqual(["s"], surface["consumers"])
        self.assertFalse(surface["structured"])

    def test_shared_surfaces_empty_for_parallel_packets(self):
        self.assertEqual([], plan_shared_surfaces(parallel_plan()))

    def test_structured_flag_follows_producer_schema(self):
        plan = consuming_plan()
        plan["schemas"] = {"OUT": {"type": "object"}}
        plan["phases"][0]["agents"][0]["schemaRef"] = "OUT"
        surfaces = plan_shared_surfaces(plan)
        self.assertTrue(surfaces[0]["structured"])

    def test_level_is_none_without_phases(self):
        self.assertEqual("none", plan_eval_level({"taskType": "planning", "phases": []}))

    def test_level_is_full_when_packets_consume_each_other(self):
        self.assertEqual("full", plan_eval_level(consuming_plan()))

    def test_level_is_inline_for_parallel_packets(self):
        self.assertEqual("inline", plan_eval_level(parallel_plan()))

    def test_level_is_full_for_multiple_code_writers(self):
        plan = parallel_plan()
        plan["taskType"] = "coding"
        for agent in plan["phases"][0]["agents"]:
            agent["role"] = "implementation"
        self.assertEqual("full", plan_eval_level(plan))

    def test_level_ignores_task_type_label(self):
        baseline = plan_eval_level(parallel_plan())
        for label in ("coding", "mixed", "review", "debugging", "planning"):
            plan = parallel_plan()
            plan["taskType"] = label
            self.assertEqual(baseline, plan_eval_level(plan), f"taskType={label} changed the level")

    def test_risk_level_ignores_task_type_label(self):
        plan = parallel_plan()
        for agent in plan["phases"][0]["agents"]:
            agent["role"] = "implementation"
        baseline = plan_risk_level(plan)
        for label in ("coding", "research", "mixed", "planning"):
            variant = parallel_plan()
            for agent in variant["phases"][0]["agents"]:
                agent["role"] = "implementation"
            variant["taskType"] = label
            self.assertEqual(baseline, plan_risk_level(variant), f"taskType={label} changed risk")

    def test_declared_risk_can_escalate_but_never_downgrade(self):
        quiet = parallel_plan()
        quiet["riskLevel"] = "high"
        self.assertEqual("high", plan_risk_level(quiet))

        risky = consuming_plan()
        risky["riskLevel"] = "low"
        self.assertEqual(plan_risk_level(consuming_plan()), plan_risk_level(risky))

    def test_normalization_decouples_risk_and_level_from_task_type(self):
        base = {
            "meta": {"name": "same shape"},
            "phases": [
                {"title": "Build", "agents": [{"label": "impl", "role": "implementation", "prompt": "p", "dependsOn": []}]}
            ],
            "schemas": {},
            "acceptance": {"required": True, "failWorkflowOnError": True, "checks": ["python_unittest", "verification_schema"]},
        }
        coding = _normalize_workflow_execution_contract({**base, "taskType": "coding"})
        research = _normalize_workflow_execution_contract({**base, "taskType": "research"})

        self.assertEqual(coding["riskLevel"], research["riskLevel"])
        self.assertEqual(coding["evalContract"]["level"], research["evalContract"]["level"])

    def test_normalization_populates_level_and_surfaces(self):
        normalized = _normalize_plan_contract(consuming_plan())
        contract = normalized["evalContract"]
        self.assertEqual("full", contract["level"])
        self.assertEqual(["a"], [item["producer"] for item in contract["sharedSurfaces"]])


if __name__ == "__main__":
    unittest.main()
