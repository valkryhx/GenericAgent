import unittest
from unittest import mock

from workflow_child_agent import NativeGPTChildAgentRunner


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

    def test_missing_required_tool_fails_closed(self):
        runner = NativeGPTChildAgentRunner()
        runner._load_tools_schema = mock.Mock(return_value=[tool("file_read")])
        runner.last_capability_snapshot = {"toolNames": ["file_read"], "mcpDiscovery": {"status": "ok"}}
        with self.assertRaisesRegex(RuntimeError, "capability_unavailable.*mcp__tavily__tavily_search"):
            runner.prepare_run_capabilities("run-2", ["mcp__tavily__tavily_search"])


if __name__ == "__main__":
    unittest.main()
