import unittest
from unittest import mock

from workflow_child_agent import NativeGPTChildAgentRunner
from workflow_tool_profiles import (
    filter_schema_for_profile,
    resolve_tool_profile,
    tool_capabilities,
)


def tool(name):
    return {"type": "function", "function": {"name": name, "parameters": {"type": "object"}}}


class WorkflowCapabilityPreflightTest(unittest.TestCase):
    def test_required_tools_are_checked_against_one_run_snapshot(self):
        runner = NativeGPTChildAgentRunner()
        calls = []
        schemas = [tool("file_read"), tool("mcp__tavily__tavily_search")]

        def load_tools(job=None):
            calls.append(job)
            runner.last_capability_snapshot = {"toolNames": ["file_read", "mcp__tavily__tavily_search"], "mcpDiscovery": {"status": "ok", "generation": "g1"}}
            return schemas

        runner._load_tools_schema = load_tools
        snapshot = runner.prepare_run_capabilities("run-1", ["file_read", "mcp__tavily__tavily_search"])
        second_snapshot = runner.prepare_run_capabilities("run-1", ["file_read"])
        self.assertEqual("g1", snapshot["mcpDiscovery"]["generation"])
        self.assertEqual(snapshot, second_snapshot)
        self.assertEqual(1, len(calls))

    def test_missing_required_tool_is_reported_not_fatal(self):
        """A disconnected MCP server degrades the run; it does not kill it.

        The old behaviour raised ``capability_unavailable`` and failed the run
        before the child could even start, which is exactly what made one dead
        search server look like a broken workflow.
        """

        runner = NativeGPTChildAgentRunner()
        runner._load_tools_schema = mock.Mock(return_value=[tool("file_read")])
        runner.last_capability_snapshot = {"toolNames": ["file_read"], "mcpDiscovery": {"status": "ok"}}
        snapshot = runner.prepare_run_capabilities("run-2", ["mcp__tavily__tavily_search"])
        report = snapshot["capabilityReport"]
        self.assertEqual(["mcp__tavily__tavily_search"], report["missingTools"])
        self.assertIn("web_search", report["unavailableCapabilities"])
        self.assertEqual({"web_search": []}, {"web_search": report["capabilityCoverage"]["web_search"]})

    def test_capability_coverage_resolves_classes_to_connected_tools(self):
        runner = NativeGPTChildAgentRunner()
        runner._load_tools_schema = mock.Mock(
            return_value=[tool("file_read"), tool("mcp__tavily__tavily_search"), tool("mcp__fetch__fetch_markdown")]
        )
        runner.last_capability_snapshot = {"toolNames": [], "mcpDiscovery": {"status": "ok"}}
        snapshot = runner.prepare_run_capabilities("run-3", [], capabilities=["web_search", "web_fetch", "execute"])
        coverage = snapshot["capabilityReport"]["capabilityCoverage"]
        self.assertEqual(["mcp__tavily__tavily_search"], coverage["web_search"])
        self.assertEqual(["mcp__fetch__fetch_markdown"], coverage["web_fetch"])
        self.assertEqual([], coverage["execute"])
        self.assertEqual(["execute", "file_write"], snapshot["unavailableCapabilities"])


class WorkflowToolProfileTest(unittest.TestCase):
    def test_research_profile_removes_execute_but_keeps_search(self):
        schema = [
            tool("file_read"),
            tool("file_write"),
            tool("code_run"),
            tool("spawn_agent"),
            tool("mcp__tavily__tavily_search"),
        ]
        names = [item["function"]["name"] for item in filter_schema_for_profile(schema, "research")]
        self.assertIn("mcp__tavily__tavily_search", names)
        self.assertIn("file_write", names)
        self.assertNotIn("code_run", names)
        self.assertNotIn("spawn_agent", names)

    def test_verify_profile_keeps_execute_and_drops_mutation(self):
        schema = [tool("file_read"), tool("file_write"), tool("code_run")]
        names = [item["function"]["name"] for item in filter_schema_for_profile(schema, "verify")]
        self.assertEqual(["file_read", "code_run"], names)

    def test_unknown_profile_raises_instead_of_widening(self):
        with self.assertRaisesRegex(ValueError, "unknown workflow tool profile"):
            resolve_tool_profile("research-ish")

    def test_mcp_tool_names_are_classified_by_behaviour(self):
        self.assertEqual({"web_search"}, set(tool_capabilities("mcp__exa__web_search_exa")))
        self.assertEqual({"file_write"}, set(tool_capabilities("mcp__office-mcp__create_document")))
        self.assertEqual({"execute"}, set(tool_capabilities("mcp__x__run_command")))


if __name__ == "__main__":
    unittest.main()
