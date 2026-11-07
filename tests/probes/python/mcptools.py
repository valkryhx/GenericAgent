from genericagent.mcp.runtime import discover_mcp_tools_cached
tools = discover_mcp_tools_cached()
for t in tools:
    fn = t.get("function", t)
    print("-", fn.get("name"))
