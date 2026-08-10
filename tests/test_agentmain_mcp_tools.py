import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class AgentMainMcpToolsTest(unittest.TestCase):
    def test_load_tool_schema_hides_agent_type_when_no_roles_are_configured(self):
        import agentmain

        with patch("subagent_roles.SubagentRoleRegistry.list_roles", return_value=[]):
            agentmain.load_tool_schema(include_mcp_tools=False)

        spawn = next(tool["function"] for tool in agentmain.TOOLS_SCHEMA if tool["function"]["name"] == "spawn_agent")
        self.assertNotIn("agent_type", spawn["parameters"]["properties"])

    def test_load_tool_schema_enumerates_only_configured_agent_types(self):
        import agentmain

        roles = [SimpleNamespace(name="auditor"), SimpleNamespace(name="researcher")]
        with patch("subagent_roles.SubagentRoleRegistry.list_roles", return_value=roles):
            agentmain.load_tool_schema(include_mcp_tools=False)

        spawn = next(tool["function"] for tool in agentmain.TOOLS_SCHEMA if tool["function"]["name"] == "spawn_agent")
        agent_type = spawn["parameters"]["properties"]["agent_type"]
        self.assertEqual(agent_type.get("enum"), ["auditor", "researcher"])
        self.assertIn("auditor", agent_type["description"])
        self.assertIn("researcher", agent_type["description"])
        self.assertIn("not a free-form label", agent_type["description"])

    def test_load_chinese_tool_schema_describes_configured_agent_types(self):
        import agentmain

        roles = [SimpleNamespace(name="researcher")]
        with patch("subagent_roles.SubagentRoleRegistry.list_roles", return_value=roles):
            agentmain.load_tool_schema("_cn", include_mcp_tools=False)

        spawn = next(tool["function"] for tool in agentmain.TOOLS_SCHEMA if tool["function"]["name"] == "spawn_agent")
        agent_type = spawn["parameters"]["properties"]["agent_type"]
        self.assertEqual(agent_type.get("enum"), ["researcher"])
        self.assertIn("不是自由标签", agent_type["description"])

    def test_load_tool_schema_can_skip_mcp_discovery(self):
        os.environ["GA_MCP_CONFIG"] = str(REPO_ROOT / "temp" / "missing-test-mcp.json")
        import agentmain

        try:
            # load_tool_schema 走的是 discover_mcp_tools_cached（带缓存），patch 它才有效。
            with patch("mcp_runtime.discover_mcp_tools_cached") as discover:
                agentmain.load_tool_schema(include_mcp_tools=False)
                discover.assert_not_called()
                names = {tool["function"]["name"] for tool in agentmain.TOOLS_SCHEMA}
        finally:
            os.environ.pop("GA_MCP_CONFIG", None)

        self.assertFalse(any(name.startswith("mcp__") for name in names))

    def test_load_tool_schema_appends_discovered_mcp_tools(self):
        os.environ["GA_MCP_CONFIG"] = str(REPO_ROOT / "temp" / "missing-test-mcp.json")
        import agentmain

        fake_tool = {
            "type": "function",
            "function": {
                "name": "mcp__demo__echo",
                "description": "[MCP: demo/echo] Echo",
                "parameters": {"type": "object", "properties": {}},
            },
        }

        try:
            # load_tool_schema 走的是 discover_mcp_tools_cached（带缓存），patch 它才有效。
            with patch("mcp_runtime.discover_mcp_tools_cached", return_value=[fake_tool]):
                agentmain.load_tool_schema()
                names = {tool["function"]["name"] for tool in agentmain.TOOLS_SCHEMA}
        finally:
            with patch("mcp_runtime.discover_mcp_tools_cached", return_value=[]):
                agentmain.load_tool_schema()
            os.environ.pop("GA_MCP_CONFIG", None)

        self.assertIn("mcp__demo__echo", names)


if __name__ == "__main__":
    unittest.main()
