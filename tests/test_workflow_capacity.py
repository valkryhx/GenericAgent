import unittest

from workflow_policy import WorkflowCapacityError, normalize_delegation_policy


class WorkflowCapacityTest(unittest.TestCase):
    def test_requested_max_agents_is_preserved_when_within_configured_limit(self):
        policy = normalize_delegation_policy(
            {"mode": "delegated", "orchestration": {"maxAgents": 12}},
            capacity=32,
        )

        self.assertEqual(12, policy["maxAgents"])
        self.assertEqual(12, policy["capacityDecision"]["requestedMaxAgents"])
        self.assertEqual(32, policy["capacityDecision"]["effectiveMaxAgents"])

    def test_capacity_rejection_is_structured_when_limit_is_exceeded(self):
        with self.assertRaisesRegex(WorkflowCapacityError, "requested 40 agents"):
            normalize_delegation_policy(
                {"mode": "delegated", "orchestration": {"maxAgents": 40}},
                capacity=16,
            )


if __name__ == "__main__":
    unittest.main()
