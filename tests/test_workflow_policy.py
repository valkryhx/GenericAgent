import unittest

from workflow_policy import build_forward_test_matrix, normalize_delegation_policy, route_workflow_mode


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
        self.assertEqual(5, policy["maxAgents"])
        self.assertEqual(4, policy["maxWaves"])
        self.assertTrue(policy["approvalRequired"])

    def test_forward_matrix_contains_direct_workflow_delegated_and_gate_cases(self):
        cases = build_forward_test_matrix()
        names = {case["name"] for case in cases}
        self.assertTrue({"direct", "workflow", "delegated", "fallback", "approval", "eval_contract"} <= names)


if __name__ == "__main__":
    unittest.main()
