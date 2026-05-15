import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class AgentMainMcpToolsTest(unittest.TestCase):
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
            with patch("mcp_runtime.discover_mcp_tools", return_value=[fake_tool]):
                agentmain.load_tool_schema()
                names = {tool["function"]["name"] for tool in agentmain.TOOLS_SCHEMA}
        finally:
            with patch("mcp_runtime.discover_mcp_tools", return_value=[]):
                agentmain.load_tool_schema()
            os.environ.pop("GA_MCP_CONFIG", None)

        self.assertIn("mcp__demo__echo", names)


if __name__ == "__main__":
    unittest.main()
