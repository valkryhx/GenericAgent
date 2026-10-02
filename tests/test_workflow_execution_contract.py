import copy
import unittest

from workflow_planner import _normalize_plan_contract, validate_workflow_plan


def executable_plan():
    return {
        "taskType": "mixed",
        "phases": [
            {"title": "Research", "agents": [{
                "label": "research", "role": "research", "prompt": "search", "dependsOn": [],
                "actions": ["search"], "requiredTools": ["mcp__tavily__tavily_search"],
                "deliverables": ["sources"],
            }]},
            {"title": "Write", "agents": [{
                "label": "writer", "role": "implementation", "prompt": "write", "dependsOn": ["research"],
                "actions": ["create_artifact"], "requiredTools": ["file_write", "file_read"],
                "writeScope": ["artifacts/page.html"], "deliverables": ["artifacts/page.html"],
                "acceptanceChecks": ["artifact_exists", "artifact_readback"],
            }]},
            {"title": "Verify", "agents": [{
                "label": "verify", "role": "verification", "prompt": "verify", "dependsOn": ["writer"],
                "actions": ["verify"], "requiredTools": ["file_read"],
            }]},
        ],
        "schemas": {},
        "executionContract": {
            "requiresExecution": True,
            "actions": [
                {"id": "search", "agent": "research"},
                {"id": "create_artifact", "agent": "writer"},
                {"id": "verify", "agent": "verify"},
            ],
            "requiredTools": ["mcp__tavily__tavily_search", "file_write", "file_read"],
            "requiredToolEvidence": [{"tool": "mcp__tavily__tavily_search", "agent": "research", "minimumCalls": 1}],
            "artifacts": [{
                "path": "artifacts/page.html", "writer": "writer",
                "requiredChecks": ["artifact_exists", "artifact_readback"],
            }],
        },
        "verification": {"level": "inline", "checks": [{
            "id": "page-exists", "kind": "artifact", "path": "artifacts/page.html", "required": True,
        }]},
    }


class WorkflowExecutionContractTest(unittest.TestCase):
    def test_validates_explicit_action_tool_artifact_contract(self):
        result = validate_workflow_plan(executable_plan())
        self.assertTrue(result["ok"], result)

    def test_rejects_execution_contract_with_planner_only_plan(self):
        plan = executable_plan()
        plan["phases"] = [{"title": "Plan", "agents": [{"label": "planner", "role": "planning", "prompt": "plan", "dependsOn": []}]}]
        result = validate_workflow_plan(plan)
        self.assertIn("plan_only_execution", {issue["code"] for issue in result["issues"]})

    def test_rejects_missing_required_tool_and_write_scope(self):
        plan = executable_plan()
        plan["phases"][0]["agents"][0]["requiredTools"] = []
        plan["phases"][1]["agents"][0]["writeScope"] = []
        result = validate_workflow_plan(plan)
        codes = {issue["code"] for issue in result["issues"]}
        self.assertIn("missing_required_tool", codes)
        self.assertIn("missing_artifact_write_scope", codes)

    def test_rejects_agent_action_missing_from_contract_mapping(self):
        plan = executable_plan()
        plan["phases"][1]["agents"][0]["actions"].append("publish")
        result = validate_workflow_plan(plan)
        self.assertIn("unmapped_agent_action", {issue["code"] for issue in result["issues"]})

    def test_rejects_tool_evidence_not_bound_to_required_agent_tool(self):
        plan = executable_plan()
        plan["executionContract"]["requiredToolEvidence"][0]["agent"] = "writer"
        result = validate_workflow_plan(plan)
        self.assertIn("invalid_tool_evidence_contract", {issue["code"] for issue in result["issues"]})

    def test_rejects_prose_instead_of_machine_checkable_artifact_checks(self):
        plan = executable_plan()
        plan["executionContract"]["artifacts"][0]["requiredChecks"] = ["文件应该存在并且能够正常打开"]
        result = validate_workflow_plan(plan)
        self.assertIn("unsupported_artifact_acceptance_check", {issue["code"] for issue in result["issues"]})

    def test_rejects_missing_artifact_checks(self):
        plan = executable_plan()
        plan["executionContract"]["artifacts"][0]["requiredChecks"] = []
        result = validate_workflow_plan(plan)
        self.assertIn("missing_artifact_acceptance_check", {issue["code"] for issue in result["issues"]})

    def test_rejects_invalid_dependencies(self):
        plan = executable_plan()
        plan["phases"][1]["agents"][0]["dependsOn"] = ["missing"]
        result = validate_workflow_plan(plan)
        self.assertIn("undefined_dependency", {issue["code"] for issue in result["issues"]})

    def test_normalizes_agent_packets_from_explicit_execution_contract(self):
        plan = executable_plan()
        agents = {
            agent["label"]: agent
            for phase in plan["phases"]
            for agent in phase["agents"]
        }
        for agent in agents.values():
            for field in ("actions", "requiredTools", "writeScope", "deliverables", "acceptanceChecks"):
                agent.pop(field, None)

        normalized = _normalize_plan_contract(plan)
        normalized_agents = {
            agent["label"]: agent
            for phase in normalized["phases"]
            for agent in phase["agents"]
        }

        self.assertEqual(normalized_agents["research"]["actions"], ["search"])
        self.assertEqual(normalized_agents["research"]["requiredTools"], ["mcp__tavily__tavily_search"])
        self.assertEqual(normalized_agents["writer"]["actions"], ["create_artifact"])
        self.assertEqual(normalized_agents["writer"]["writeScope"], ["artifacts/page.html"])
        self.assertEqual(normalized_agents["writer"]["deliverables"], ["artifacts/page.html"])
        self.assertEqual(
            set(normalized_agents["writer"]["acceptanceChecks"]),
            {"artifact_exists", "artifact_readback"},
        )
        validation = validate_workflow_plan(normalized)
        self.assertTrue(validation["ok"], validation)

    def test_deduplicates_tool_evidence_from_generic_command_checks(self):
        plan = executable_plan()
        plan["verification"]["checks"].append({
            "id": "required_tool_evidence", "kind": "command", "required": True, "owner": "host",
        })

        normalized = _normalize_plan_contract(plan)

        self.assertNotIn("required_tool_evidence", {check["id"] for check in normalized["verification"]["checks"]})
        validation = validate_workflow_plan(normalized)
        self.assertTrue(validation["ok"], validation)

    def test_normalizes_machine_artifact_check_ids_to_artifact_adapter(self):
        plan = executable_plan()
        plan["verification"]["checks"].append({
            "id": "source_count", "kind": "schema", "schemaRef": "source_count", "required": True,
        })
        normalized = _normalize_plan_contract(plan)
        check = next(item for item in normalized["verification"]["checks"] if item["id"] == "source_count")
        self.assertEqual(check["kind"], "artifact")
        self.assertEqual(check["path"], "artifacts/page.html")

    def test_artifact_content_checks_are_advisory_in_normalized_plan(self):
        plan = executable_plan()
        plan["verification"]["checks"].extend([
            {"id": "artifact_structure", "kind": "artifact", "required": True},
            {"id": "source_count", "kind": "artifact", "required": True},
        ])

        normalized = _normalize_plan_contract(plan)
        checks = {check["id"]: check for check in normalized["verification"]["checks"]}

        self.assertFalse(checks["artifact_structure"]["required"])
        self.assertFalse(checks["source_count"]["required"])
        self.assertTrue(checks["artifact_structure"]["advisory"])
        self.assertTrue(checks["source_count"]["advisory"])

    def test_binds_artifact_verification_checks_to_explicit_artifact_path(self):
        plan = executable_plan()
        plan["verification"] = {
            "level": "full",
            "checks": [
                {"id": "artifact_exists", "kind": "artifact", "required": True, "owner": "verify"},
                {"id": "artifact_readback", "kind": "artifact", "required": True, "owner": "verify"},
            ],
            "independentReview": False,
        }
        normalized = _normalize_plan_contract(plan)
        checks = {check["id"]: check for check in normalized["verification"]["checks"]}
        self.assertEqual(checks["artifact_exists"]["path"], "artifacts/page.html")
        self.assertEqual(checks["artifact_readback"]["path"], "artifacts/page.html")
        self.assertTrue(validate_workflow_plan(normalized)["ok"])

    def test_normalizes_artifact_alias_on_semantic_checks(self):
        plan = executable_plan()
        plan["executionContract"]["artifacts"] = [
            {
                "path": "artifacts/research.json",
                "writer": "researcher",
                "requiredChecks": ["artifact_exists"],
            },
            {
                "path": "artifacts/final.html",
                "writer": "writer",
                "requiredChecks": ["artifact_exists"],
            },
        ]
        plan["verification"] = {
            "level": "inline",
            "checks": [
                {
                    "id": "check_primary_exists",
                    "kind": "artifact",
                    "artifact": "artifacts/research.json",
                    "check": "artifact_exists",
                    "required": True,
                    "owner": "host",
                },
            ],
            "independentReview": False,
        }

        normalized = _normalize_plan_contract(plan)
        check = normalized["verification"]["checks"][0]
        self.assertEqual(check["path"], "artifacts/research.json")
        self.assertEqual(check["kind"], "artifact")

    def test_defers_unbound_artifact_checks_to_execution_contract(self):
        plan = executable_plan()
        plan["executionContract"]["artifacts"] = [
            {
                "path": "research/sources.json",
                "writer": "researcher",
                "requiredChecks": ["artifact_exists", "artifact_readback"],
            },
            {
                "path": "profile/page.html",
                "writer": "Research Synthesis and HTML Builder",
                "requiredChecks": ["artifact_exists", "artifact_readback"],
            },
        ]
        plan["verification"] = {
            "level": "inline",
            "checks": [
                # Pathless, and the id matches no declared artifact check id.
                # The model must not be able to smuggle an unbound required
                # artifact check past the host; the execution contract is the
                # authoritative source and already enforces these paths.
                {"id": "sources_json_exists", "kind": "artifact", "required": True, "owner": "researcher"},
                {"id": "html_exists", "kind": "artifact", "required": True, "owner": "Research Synthesis and HTML Builder"},
            ],
            "independentReview": False,
        }

        normalized = _normalize_plan_contract(plan)
        ids = {check["id"] for check in normalized["verification"]["checks"]}
        self.assertNotIn("sources_json_exists", ids)
        self.assertNotIn("html_exists", ids)

    def test_binds_artifact_checks_by_declared_required_check_id(self):
        plan = executable_plan()
        plan["executionContract"]["artifacts"] = [
            {
                "path": "research/sources.json",
                "writer": "researcher",
                "requiredChecks": ["artifact_exists", "artifact_readback"],
            },
            {
                "path": "profile/page.html",
                "writer": "writer",
                "requiredChecks": ["artifact_exists", "artifact_readback"],
            },
        ]
        plan["verification"] = {
            "level": "inline",
            "checks": [
                # artifact_readback is declared by both artifacts; the host
                # must still refuse to guess which path it meant.
                {"id": "artifact_readback", "kind": "artifact", "required": True},
                {"id": "artifact", "kind": "artifact", "required": True, "path": "profile/page.html"},
            ],
            "independentReview": False,
        }

        normalized = _normalize_plan_contract(plan)
        checks = {check["id"]: check for check in normalized["verification"]["checks"]}
        self.assertNotIn("artifact_readback", checks)
        self.assertEqual(checks["artifact"]["path"], "profile/page.html")

    def test_derives_verification_checks_when_model_leaves_view_empty(self):
        plan = executable_plan()
        plan["verification"] = {"level": "inline", "checks": [], "independentReview": False}

        normalized = _normalize_plan_contract(plan)
        checks = normalized["verification"]["checks"]
        requirements = {(check["path"], check["check"]) for check in checks}
        self.assertIn(("artifacts/page.html", "artifact_exists"), requirements)
        self.assertIn(("artifacts/page.html", "artifact_readback"), requirements)
        self.assertTrue(all(check["required"] for check in checks))
        self.assertTrue(all(check["kind"] == "artifact" for check in checks))
        self.assertTrue(validate_workflow_plan(normalized)["ok"], validate_workflow_plan(normalized))

    def test_unbacked_schema_check_becomes_advisory(self):
        # A model may restate an artifact's schema requirement as a standalone
        # verification check with no independent evidence producer (no
        # verification/review agent and no evidence payload). GA has no host
        # schema evaluator wired to plan schemas, so treating it as a hard gate
        # fails runs that wrote a perfectly valid artifact. Keep it visible but
        # advisory; the authoritative requirement stays on the execution
        # contract's artifact ``schema_valid`` entry.
        plan = executable_plan()
        plan["executionContract"]["artifacts"][0]["requiredChecks"] = [
            "artifact_exists", "artifact_readback", "schema_valid",
        ]
        plan["verification"] = {
            "level": "inline",
            "checks": [
                {"id": "page_json_schema", "kind": "schema", "schemaRef": "SOURCE_SCHEMA", "required": True, "owner": "host"},
            ],
            "independentReview": False,
        }

        normalized = _normalize_plan_contract(plan)
        check = next(item for item in normalized["verification"]["checks"] if item["id"] == "page_json_schema")
        self.assertFalse(check["required"])
        self.assertTrue(check.get("advisory"))

    def test_normalizes_object_and_string_agent_actions_to_unique_ids(self):
        plan = executable_plan()
        research = plan["phases"][0]["agents"][0]
        research["actions"] = [
            {"id": "search", "tool": "mcp__tavily__tavily_search"},
            "search",
        ]

        normalized = _normalize_plan_contract(plan)
        normalized_research = normalized["phases"][0]["agents"][0]

        self.assertEqual(normalized_research["actions"], ["search"])
        validation = validate_workflow_plan(normalized)
        self.assertTrue(validation["ok"], validation)


if __name__ == "__main__":
    unittest.main()
