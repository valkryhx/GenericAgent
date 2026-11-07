import time
from genericagent.mcp.runtime import mcp_status

t0 = time.time()
st = mcp_status(timeout=60)
el = time.time() - t0
for s in st.get("servers", []):
    print("%-8s %-10s tools=%d  %.2fs" % (s["name"], s["status"], s["tool_count"], el))
print("total %.2fs" % el)
