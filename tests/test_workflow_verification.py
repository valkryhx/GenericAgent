import unittest

from workflow_verification import normalize_verification_contract, validate_verification_contract


class WorkflowVerificationContractTest(unittest.TestCase):
    def test_inline_contract_does_not_require_verification_role(self):
        plan = {
            "agents": [{"id": "impl", "role": "implementation", "writeScope": ["src"]}],
            "verification": {
                "level": "inline",
                "checks": [
                    {
                        "id": "targeted",
                        "kind": "command",
                        "required": True,
                        "owner": "host",
                        "command": ["python", "-m", "unittest", "tests.test_x"],
                    }
                ],
            },
        }

        contract = normalize_verification_contract(plan)

        self.assertEqual(contract["level"], "inline")
        self.assertFalse(contract["independentReview"])
        self.assertEqual(contract["checks"][0]["kind"], "command")
        self.assertFalse(contract["metadata"]["legacyConverted"])

    def test_legacy_fields_convert_without_forcing_new_role(self):
        plan = {
            "agents": [{"id": "impl", "role": "implementation"}],
            "acceptance": {"required": True, "checks": ["python_unittest", "verification_schema"]},
        }

        contract = normalize_verification_contract(plan)

        self.assertEqual(
            {check["id"] for check in contract["checks"]},
            {"legacy-python-unittest", "legacy-verification-schema"},
        )
        self.assertTrue(contract["metadata"]["legacyConverted"])
        self.assertFalse(contract["independentReview"])

    def test_verification_role_requests_independent_review_in_legacy_plan(self):
        plan = {
            "agents": [{"id": "review", "role": "verification"}],
            "acceptance": {"required": True, "checks": ["verification_schema"]},
        }

        contract = normalize_verification_contract(plan)

        self.assertEqual(contract["level"], "full")
        self.assertTrue(contract["independentReview"])

    def test_agent_label_is_a_valid_verification_owner(self):
        plan = {
            "phases": [{"agents": [{"label": "Real MCP World Cup Results Research Agent"}]}],
            "verification": {
                "level": "inline",
                "checks": [
                    {
                        "id": "mcp-evidence",
                        "kind": "schema",
                        "required": True,
                        "owner": "Real MCP World Cup Results Research Agent",
                        "schemaRef": "mcp_result",
                    }
                ],
            },
        }

        contract = normalize_verification_contract(plan)

        self.assertEqual("Real MCP World Cup Results Research Agent", contract["checks"][0]["owner"])

    def test_invalid_owner_control_characters_are_rejected(self):
        contract = {
            "level": "inline",
            "checks": [
                {
                    "id": "bad-owner",
                    "kind": "artifact",
                    "required": True,
                    "owner": "agent\nforged",
                    "path": "report.md",
                }
            ],
            "independentReview": False,
        }

        with self.assertRaisesRegex(ValueError, "unsupported verification check owner"):
            validate_verification_contract(contract)

    def test_invalid_check_kind_is_rejected(self):
        contract = {
            "level": "inline",
            "checks": [{"id": "bad", "kind": "unknown", "required": True, "owner": "host"}],
            "independentReview": False,
        }

        with self.assertRaisesRegex(ValueError, "unsupported verification check kind"):
            validate_verification_contract(contract)


if __name__ == "__main__":
    unittest.main()
