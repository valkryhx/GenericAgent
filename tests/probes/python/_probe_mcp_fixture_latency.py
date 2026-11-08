"""Measure how long the MCP fixture takes to reach the tool call.

Reported symptom: the abort test fails 5/5 when run alone ("MCP tool fixture did
not start" after a 5s wait) but passes inside the full module run. The test waits
5 seconds for a marker the server writes *inside* the tool, so a cold-process MCP
handshake longer than 5s produces exactly that failure and nothing else.

This measures the real latency from a cold interpreter, which is the state a
single-test run starts in.
"""

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

SERVER = """import os
import time
from pathlib import Path
from fastmcp import FastMCP
Path({pid!r}).write_text(str(os.getpid()), encoding='utf-8')
mcp = FastMCP('cancellable')
@mcp.tool(description='Block long enough for cancellation.')
def hang(text: str = 'x') -> str:
    Path({marker!r}).write_text('started', encoding='utf-8')
    time.sleep(30.0)
    return 'done:' + text
if __name__ == '__main__':
    mcp.run(transport='stdio', show_banner=False)
"""

DRIVER = (
    "import sys, time\n"
    "from pathlib import Path\n"
    f"sys.path.insert(0, {str(REPO)!r})\n"
    f"os.environ['GA_MCP_CONFIG'] = {None!r}\n"
)


def run_once(root, label):
    pid_path = root / f"server_{label}.pid"
    marker = root / f"marker_{label}.txt"
    script = root / f"srv_{label}.py"
    script.write_text(SERVER.format(pid=str(pid_path), marker=str(marker)), encoding="utf-8")
    config = root / f"mcp_{label}.json"
    config.write_text(
        json.dumps({"mcpServers": {"cancellable": {"type": "stdio", "command": sys.executable, "args": [str(script)]}}}),
        encoding="utf-8",
    )
    code = (
        "import os, sys, time, threading\n"
        "from pathlib import Path\n"
        f"os.environ['GA_MCP_CONFIG'] = {str(config)!r}\n"
        f"sys.path.insert(0, {str(REPO)!r})\n"
        "from genericagent.core.loop import exhaust\n"
        "from genericagent.core.handler import GenericAgentHandler\n"
        "class Agent:\n"
        "    is_running=True; stop_sig=False; llmclient=None; task_dir=None\n"
        "agent = Agent()\n"
        f"handler = GenericAgentHandler(agent, last_history=[], cwd={str(root)!r})\n"
        "agent.handler = handler\n"
        "def run():\n"
        "    try:\n"
        "        exhaust(handler.dispatch('mcp__cancellable__hang', {'text':'x'},"
        " type('R',(),{'content':''})()))\n"
        "    except BaseException:\n"
        "        pass\n"
        f"start = time.monotonic()\n"
        "threading.Thread(target=run, daemon=True).start()\n"
        f"marker = Path({str(marker)!r})\n"
        "deadline = start + 60\n"
        "while not marker.exists() and time.monotonic() < deadline:\n"
        "    time.sleep(0.02)\n"
        f"print('SECONDS_TO_MARKER' if marker.exists() else 'TIMEOUT', round(time.monotonic()-start, 2))\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO)
    env["TEMP"] = r"D:\tmp"
    env["TMP"] = r"D:\tmp"
    started = time.monotonic()
    proc = subprocess.run([sys.executable, "-c", code], cwd=str(REPO), capture_output=True, text=True, env=env, timeout=180)
    wall = time.monotonic() - started
    line = (proc.stdout or "").strip().splitlines()
    return (line[-1] if line else "<no output>"), round(wall, 2), (proc.stderr or "").strip().splitlines()[-2:]


def main():
    with tempfile.TemporaryDirectory(dir=r"D:\tmp") as td:
        root = Path(td)
        for i in range(3):
            verdict, wall, tail = run_once(root, f"r{i}")
            print(f"run {i}: {verdict}   wall={wall}s")
            if tail:
                print("   stderr:", tail)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
