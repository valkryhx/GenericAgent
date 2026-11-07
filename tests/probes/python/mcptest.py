import json
from genericagent.mcp.runtime import mcp_status, default_mcp_config_path

print("config:", default_mcp_config_path())
st = mcp_status(timeout=45)
for s in st.get("servers", []):
    print(json.dumps(s, ensure_ascii=False))
