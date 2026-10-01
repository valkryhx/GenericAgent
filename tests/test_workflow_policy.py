import unittest

from workflow_policy import (
    build_forward_test_matrix,
    normalize_delegation_policy,
    plan_required_waves,
    route_workflow_mode,
)


def deep_tdd_plan():
    return {
        "taskType": "coding",
        "riskLevel": "medium",
        "phases": [
            {"title": "Understand", "agents": [{"label": "a", "dependsOn": []}]},
            {"title": "Tests", "agents": [{"label": "b", "dependsOn": ["a"]}]},
            {"title": "Implementation", "agents": [{"label": "c", "dependsOn": ["b"]}]},
            {"title": "Verification", "agents": [{"label": "d", "dependsOn": ["c"]}]},
            {"title": "Summary", "agents": [{"label": "e", "dependsOn": ["d"]}]},
        ],
    }


class WorkflowPolicyTest(unittest.TestCase):
    def test_routes_simple_low_risk_task_to_direct_mode(self):
        self.assertEqual(
            "direct",
            route_workflow_mode(task_type="planning", requested_mode=None, phase_count=0, risk_level="low"),
        )

    def test_routes_bounded_delegation_to_delegated_mode(self):
        policy = normalize_delegation_policy(
            {"mode": "delegated", "orchestration": {"maxAgents": 99, "maxWaves": 99, "delegationAllowed": True}}
        )
        self.assertEqual("delegated", policy["mode"])
        self.assertEqual(99, policy["maxAgents"])
        self.assertEqual(64, policy["maxWaves"])
        self.assertTrue(policy["approvalRequired"])

    def test_workflow_mode_is_not_capped_by_delegated_sidecar_budget(self):
        plan = deep_tdd_plan()
        plan["orchestration"] = {"maxAgents": 5, "maxWaves": 4}

        policy = normalize_delegation_policy(plan)

        self.assertEqual("workflow", policy["mode"])
        self.assertEqual(5, policy["maxAgents"])
        # A 5-stage TDD chain needs 5 waves; the old hard cap of 4 killed it.
        self.assertEqual(5, policy["maxWaves"])

    def test_delegated_mode_keeps_its_bounded_sidecar_budget(self):
        plan = deep_tdd_plan()
        plan["mode"] = "delegated"

        policy = normalize_delegation_policy(plan)

        self.assertEqual("delegated", policy["mode"])
        self.assertEqual(5, policy["maxAgents"])
        self.assertEqual(5, policy["maxWaves"])

    def test_plan_required_waves_counts_the_longest_dependency_chain(self):
        self.assertEqual(5, plan_required_waves(deep_tdd_plan()))
        self.assertEqual(0, plan_required_waves({"phases": []}))
        parallel = {
            "phases": [{"title": "Collect", "agents": [{"label": "a"}, {"label": "b"}]}]
        }
        self.assertEqual(1, plan_required_waves(parallel))

    def test_host_budget_never_undercuts_the_plan_it_accepted(self):
        plan = deep_tdd_plan()
        plan["orchestration"] = {"maxAgents": 1, "maxWaves": 1}

        policy = normalize_delegation_policy(plan)

        self.assertEqual(5, policy["maxAgents"])
        self.assertEqual(5, policy["maxWaves"])

    def test_forward_matrix_contains_direct_workflow_delegated_and_gate_cases(self):
        cases = build_forward_test_matrix()
        names = {case["name"] for case in cases}
        self.assertTrue({"direct", "workflow", "delegated", "fallback", "approval", "eval_contract"} <= names)

    def test_router_downgrades_declared_workflow_for_phase_less_small_task(self):
        self.assertEqual(
            "direct",
            route_workflow_mode(
                task_type="planning",
                requested_mode="workflow",
                phase_count=0,
                risk_level="low",
            ),
        )

    def test_router_keeps_declared_workflow_when_plan_has_real_phases(self):
        self.assertEqual(
            "workflow",
            route_workflow_mode(
                task_type="review",
                requested_mode="workflow",
                phase_count=2,
                risk_level="medium",
            ),
        )


if __name__ == "__main__":
    unittest.main()
