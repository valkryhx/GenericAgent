"""Capture the full stderr of the FastMCP stdio server the MCP tests spawn.

The tests only assert on the absence of a tool ("not found in {}"), which hides
the server's own traceback. This runs the same script and dumps the whole
stderr, plus the installed fastmcp version, so the real exception is visible.
"""

import subprocess
import sys
import tempfile
from pathlib import Path

SERVER = (
    "from fastmcp import FastMCP\n"
    "mcp = FastMCP('demo')\n"
    "@mcp.tool(description='Echo text through the demo MCP server.')\n"
    "def echo(text: str) -> str:\n"
    "    return 'echo:' + text\n"
    "if __name__ == '__main__':\n"
    "    mcp.run(transport='stdio', show_banner=False)\n"
)


def main():
    print("python:", sys.executable)
    try:
        import fastmcp

        print("fastmcp:", getattr(fastmcp, "__version__", "?"), fastmcp.__file__)
    except Exception as exc:
        print("fastmcp import failed:", exc)
        return 1

    with tempfile.TemporaryDirectory(dir=r"D:\tmp") as td:
        script = Path(td) / "demo_mcp_server.py"
        script.write_text(SERVER, encoding="utf-8")
        init = (
            '{"jsonrpc":"2.0","id":1,"method":"initialize","params":'
            '{"protocolVersion":"2024-11-05","capabilities":{},'
            '"clientInfo":{"name":"probe","version":"0"}}}\n'
        )
        proc = subprocess.run(
            [sys.executable, str(script)],
            input=init, capture_output=True, text=True, timeout=60,
        )
        print("returncode:", proc.returncode)
        print("--- full stderr ---")
        print(proc.stderr or "<empty>")
        print("--- end stderr ---")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
