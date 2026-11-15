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
    def test_spawn_agent_offers_no_closed_set_of_agent_types(self):
        """The tool must not publish a value menu for role selection.

        `agent_type` used to be the only enum-constrained property on an otherwise
        all-optional tool (3 role names plus a 304-char catalog injected at runtime).
        In the 2026-11-15 session that made it the sole output anchor: 15 of 24 calls
        arrived as `{'agent_type': 'explore'}` with every other key gone. A role only
        rewrites the child's prompt prefix, which is not worth a closed list.
        """
        from genericagent.core import app as agentmain

        agentmain.load_tool_schema(include_mcp_tools=False)

        spawn = next(tool["function"] for tool in agentmain.TOOLS_SCHEMA if tool["function"]["name"] == "spawn_agent")
        properties = spawn["parameters"]["properties"]
        self.assertNotIn("agent_type", properties)
        role = properties.get("role")
        self.assertIsNotNone(role, "spawn_agent must still accept a role")
        self.assertNotIn("enum", role, "a role is optional guidance, not a closed menu")

    def test_role_description_names_the_available_roles_without_a_closed_list(self):
        """A role stays discoverable by name; it just is not a menu the model must fill.

        A bare "role" property with an empty description would be useless (the model
        cannot guess `explore` exists), so the available names stay in the prose. The
        difference from `agent_type` is that the value is not an enum and the field is
        not the only constrained property on the tool.
        """
        from genericagent.core import app as agentmain

        roles = [SimpleNamespace(name="auditor"), SimpleNamespace(name="researcher")]
        with patch("genericagent.agents.roles.SubagentRoleRegistry.list_roles", return_value=roles):
            agentmain.load_tool_schema(include_mcp_tools=False)

        spawn = next(tool["function"] for tool in agentmain.TOOLS_SCHEMA if tool["function"]["name"] == "spawn_agent")
        role = spawn["parameters"]["properties"]["role"]
        self.assertNotIn("enum", role)
        self.assertIn("auditor", role["description"])
        self.assertIn("researcher", role["description"])

    def test_load_chinese_tool_schema_describes_roles(self):
        from genericagent.core import app as agentmain

        roles = [SimpleNamespace(name="researcher")]
        with patch("genericagent.agents.roles.SubagentRoleRegistry.list_roles", return_value=roles):
            agentmain.load_tool_schema("_cn", include_mcp_tools=False)

        spawn = next(tool["function"] for tool in agentmain.TOOLS_SCHEMA if tool["function"]["name"] == "spawn_agent")
        role = spawn["parameters"]["properties"]["role"]
        self.assertIn("researcher", role["description"])

    def test_load_tool_schema_publishes_the_builtin_role_catalog(self):
        """The builtin roles must stay discoverable from the tool description."""
        from genericagent.core import app as agentmain

        agentmain.load_tool_schema(include_mcp_tools=False)

        spawn = next(tool["function"] for tool in agentmain.TOOLS_SCHEMA if tool["function"]["name"] == "spawn_agent")
        role = spawn["parameters"]["properties"]["role"]
        self.assertNotIn("enum", role)
        for name in ("explore", "plan", "review"):
            self.assertIn(name, role["description"])
        # The catalog must not imply a role restricts tools: it does not.
        self.assertIn("does not change which tools", role["description"])
        self.assertNotIn("read-only", role["description"])

    def test_spawn_agent_schema_declares_task_name_and_message_required(self):
        """An all-optional spawn_agent taught the model nothing about what it must send.

        Real failure (2026-11-15, installed 0.6.1): 15 of 24 `spawn_agent` calls arrived
        as `{'agent_type': 'explore'}` with every other key missing. The schema declared
        18 properties and no `required`, so the model only discovered that `message` was
        mandatory by spending three minutes retrying against `{"msg": "message is
        required"}`. Declaring the contract on the schema is what stops the loop.
        """
        from genericagent.core import app as agentmain

        agentmain.load_tool_schema(include_mcp_tools=False)

        spawn = next(tool["function"] for tool in agentmain.TOOLS_SCHEMA if tool["function"]["name"] == "spawn_agent")
        required = spawn["parameters"].get("required") or []
        self.assertIn("message", required)
        self.assertIn("task_name", required)

    def test_load_tool_schema_can_skip_mcp_discovery(self):
        os.environ["GA_MCP_CONFIG"] = str(REPO_ROOT / "temp" / "missing-test-mcp.json")
        from genericagent.core import app as agentmain

        try:
            # load_tool_schema 走的是 discover_mcp_tools_cached_fast（带缓存且不阻塞），patch 它才有效。
            with patch("genericagent.mcp.runtime.discover_mcp_tools_cached_fast") as discover:
                agentmain.load_tool_schema(include_mcp_tools=False)
                discover.assert_not_called()
                names = {tool["function"]["name"] for tool in agentmain.TOOLS_SCHEMA}
        finally:
            os.environ.pop("GA_MCP_CONFIG", None)

        self.assertFalse(any(name.startswith("mcp__") for name in names))

    def test_load_tool_schema_appends_discovered_mcp_tools(self):
        os.environ["GA_MCP_CONFIG"] = str(REPO_ROOT / "temp" / "missing-test-mcp.json")
        from genericagent.core import app as agentmain

        fake_tool = {
            "type": "function",
            "function": {
                "name": "mcp__demo__echo",
                "description": "[MCP: demo/echo] Echo",
                "parameters": {"type": "object", "properties": {}},
            },
        }

        try:
            # load_tool_schema 走的是 discover_mcp_tools_cached_fast（带缓存且不阻塞），patch 它才有效。
            with patch("genericagent.mcp.runtime.discover_mcp_tools_cached_fast", return_value=[fake_tool]):
                agentmain.load_tool_schema()
                names = {tool["function"]["name"] for tool in agentmain.TOOLS_SCHEMA}
        finally:
            with patch("genericagent.mcp.runtime.discover_mcp_tools_cached_fast", return_value=[]):
                agentmain.load_tool_schema()
            os.environ.pop("GA_MCP_CONFIG", None)

        self.assertIn("mcp__demo__echo", names)


if __name__ == "__main__":
    unittest.main()
