import asyncio
import json
import os
import shutil
import sys
import threading
import time
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from agent_loop import exhaust  # noqa: E402
from agentmain import GenericAgent  # noqa: E402
from ga import GenericAgentHandler  # noqa: E402
from mcp_runtime import (  # noqa: E402
    build_mcp_tool_name,
    call_mcp_tool,
    clear_mcp_cache,
    discover_mcp_tools_cached,
    discover_mcp_tools,
    disable_mcp_server,
    enable_mcp_server,
    get_mcp_manager,
    load_mcp_config,
    mcp_cancellation_scope,
    mcp_status,
    normalize_mcp_name,
    reconnect_mcp_server,
    reset_mcp_manager,
    set_mcp_server_enabled,
    _MCP_LOG_DIR,
    _redact_sensitive,
)


TEMP_ROOT = REPO_ROOT / "temp" / "test_mcp_runtime"
TEMP_ROOT.mkdir(parents=True, exist_ok=True)


@contextmanager
def _tempdir():
    path = TEMP_ROOT / f"case_{uuid.uuid4().hex}"
    path.mkdir(parents=True)
    try:
        yield str(path)
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _process_exists(pid: int) -> bool:
    if os.name != "nt":
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    import ctypes

    handle = ctypes.windll.kernel32.OpenProcess(0x00100000, False, int(pid))
    if not handle:
        return False
    try:
        return ctypes.windll.kernel32.WaitForSingleObject(handle, 0) == 258
    finally:
        ctypes.windll.kernel32.CloseHandle(handle)


def _wait_for_process_exit(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not _process_exists(pid):
            return True
        time.sleep(0.05)
    return not _process_exists(pid)


async def _pending_manager_tasks() -> list[str]:
    await asyncio.sleep(0)
    current = asyncio.current_task()
    return [
        repr(task.get_coro())
        for task in asyncio.all_tasks()
        if task is not current and not task.done()
    ]


def _write_demo_server(tmp_path: Path) -> Path:
    script_path = tmp_path / "demo_mcp_server.py"
    script_path.write_text(
        "from fastmcp import FastMCP\n"
        "mcp = FastMCP('demo')\n"
        "@mcp.tool(description='Echo text through the demo MCP server.')\n"
        "def echo(text: str) -> str:\n"
        "    return 'echo:' + text\n"
        "if __name__ == '__main__':\n"
        "    mcp.run(transport='stdio', show_banner=False)\n",
        encoding="utf-8",
    )
    return script_path


def _write_chinese_stderr_server(tmp_path: Path) -> Path:
    script_path = tmp_path / "chinese_stderr_mcp_server.py"
    script_path.write_text(
        "import sys\n"
        "from fastmcp import FastMCP\n"
        "print('中文stderr启动日志', file=sys.stderr, flush=True)\n"
        "mcp = FastMCP('zhdemo')\n"
        "@mcp.tool(description='Return Chinese text.')\n"
        "def hello() -> str:\n"
        "    print('中文stderr调用日志', file=sys.stderr, flush=True)\n"
        "    return '中文结果'\n"
        "if __name__ == '__main__':\n"
        "    mcp.run(transport='stdio', show_banner=False)\n",
        encoding="utf-8",
    )
    return script_path


def _write_long_description_server(tmp_path: Path) -> Path:
    script_path = tmp_path / "long_description_mcp_server.py"
    long_description = "A" * 3000
    script_path.write_text(
        "from fastmcp import FastMCP\n"
        "mcp = FastMCP('longdesc')\n"
        f"@mcp.tool(description={long_description!r})\n"
        "def echo(text: str) -> str:\n"
        "    return text\n"
        "if __name__ == '__main__':\n"
        "    mcp.run(transport='stdio', show_banner=False)\n",
        encoding="utf-8",
    )
    return script_path


def _write_counting_server(tmp_path: Path) -> Path:
    counter_path = tmp_path / "starts.txt"
    script_path = tmp_path / "counting_mcp_server.py"
    script_path.write_text(
        "from pathlib import Path\n"
        "from fastmcp import FastMCP\n"
        f"counter = Path({str(counter_path)!r})\n"
        "counter.write_text(str(int(counter.read_text() or '0') + 1) if counter.exists() else '1')\n"
        "mcp = FastMCP('counting')\n"
        "@mcp.tool(description='Echo text.')\n"
        "def echo(text: str) -> str:\n"
        "    return 'echo:' + text\n"
        "if __name__ == '__main__':\n"
        "    mcp.run(transport='stdio', show_banner=False)\n",
        encoding="utf-8",
    )
    return script_path



def _write_hanging_server(tmp_path: Path, sleep_seconds: float = 30.0) -> Path:
    script_path = tmp_path / "hanging_mcp_server.py"
    script_path.write_text(
        "import time\n"
        "from fastmcp import FastMCP\n"
        "mcp = FastMCP('hanging')\n"
        "@mcp.tool(description='Sleep for a long time before returning.')\n"
        "def hang(text: str = 'x') -> str:\n"
        f"    time.sleep({float(sleep_seconds)!r})\n"
        "    return 'done:' + text\n"
        "if __name__ == '__main__':\n"
        "    mcp.run(transport='stdio', show_banner=False)\n",
        encoding="utf-8",
    )
    return script_path


def _write_hanging_startup_server(tmp_path: Path, sleep_seconds: float = 10.0) -> tuple[Path, Path, Path]:
    marker_path = tmp_path / "startup_server_started.txt"
    pid_path = tmp_path / "startup_server.pid"
    script_path = tmp_path / "hanging_startup_mcp_server.py"
    script_path.write_text(
        "import os\n"
        "import time\n"
        "from pathlib import Path\n"
        f"Path({str(pid_path)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
        f"Path({str(marker_path)!r}).write_text('started', encoding='utf-8')\n"
        f"time.sleep({float(sleep_seconds)!r})\n",
        encoding="utf-8",
    )
    return script_path, marker_path, pid_path


def _write_cancellable_server(tmp_path: Path, sleep_seconds: float = 30.0) -> tuple[Path, Path, Path]:
    call_marker = tmp_path / "tool_call_started.txt"
    pid_path = tmp_path / "server.pid"
    script_path = tmp_path / "cancellable_mcp_server.py"
    script_path.write_text(
        "import os\n"
        "import time\n"
        "from pathlib import Path\n"
        "from fastmcp import FastMCP\n"
        f"Path({str(pid_path)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
        "mcp = FastMCP('cancellable')\n"
        "@mcp.tool(description='Block long enough for cancellation.')\n"
        "def hang(text: str = 'x') -> str:\n"
        f"    Path({str(call_marker)!r}).write_text('started', encoding='utf-8')\n"
        f"    time.sleep({float(sleep_seconds)!r})\n"
        "    return 'done:' + text\n"
        "if __name__ == '__main__':\n"
        "    mcp.run(transport='stdio', show_banner=False)\n",
        encoding="utf-8",
    )
    return script_path, call_marker, pid_path


def _write_reconnect_after_timeout_server(tmp_path: Path) -> tuple[Path, Path, Path]:
    starts_path = tmp_path / "timeout_server_starts.txt"
    pid_path = tmp_path / "timeout_server.pid"
    script_path = tmp_path / "timeout_reconnect_mcp_server.py"
    script_path.write_text(
        "import os\n"
        "import time\n"
        "from pathlib import Path\n"
        "from fastmcp import FastMCP\n"
        f"starts = Path({str(starts_path)!r})\n"
        "starts.write_text(str(int(starts.read_text() or '0') + 1) if starts.exists() else '1', encoding='utf-8')\n"
        f"Path({str(pid_path)!r}).write_text(str(os.getpid()), encoding='utf-8')\n"
        "mcp = FastMCP('timeout-reconnect')\n"
        "@mcp.tool(description='Sleep for the requested delay.')\n"
        "def delay(text: str, seconds: float = 0) -> str:\n"
        "    time.sleep(float(seconds))\n"
        "    return 'done:' + text\n"
        "if __name__ == '__main__':\n"
        "    mcp.run(transport='stdio', show_banner=False)\n",
        encoding="utf-8",
    )
    return script_path, starts_path, pid_path


def _write_delayed_counting_server(tmp_path: Path, startup_delay: float = 1.0) -> tuple[Path, Path]:
    starts_path = tmp_path / "delayed_server_starts.txt"
    script_path = tmp_path / "delayed_counting_mcp_server.py"
    script_path.write_text(
        "import time\n"
        "from pathlib import Path\n"
        f"starts = Path({str(starts_path)!r})\n"
        "starts.write_text(str(int(starts.read_text() or '0') + 1) if starts.exists() else '1', encoding='utf-8')\n"
        f"time.sleep({float(startup_delay)!r})\n"
        "from fastmcp import FastMCP\n"
        "mcp = FastMCP('delayed')\n"
        "@mcp.tool(description='Echo after delayed startup.')\n"
        "def echo(text: str) -> str:\n"
        "    return 'echo:' + text\n"
        "if __name__ == '__main__':\n"
        "    mcp.run(transport='stdio', show_banner=False)\n",
        encoding="utf-8",
    )
    return script_path, starts_path


def _write_failing_marker_server(tmp_path: Path) -> tuple[Path, Path]:
    marker_path = tmp_path / "bad_server_started.txt"
    script_path = tmp_path / "failing_mcp_server.py"
    script_path.write_text(
        "from pathlib import Path\n"
        f"Path({str(marker_path)!r}).write_text('started', encoding='utf-8')\n"
        "raise SystemExit(2)\n",
        encoding="utf-8",
    )
    return script_path, marker_path


def _write_failing_counting_server(tmp_path: Path) -> tuple[Path, Path]:
    counter_path = tmp_path / "bad_server_starts.txt"
    script_path = tmp_path / "failing_counting_mcp_server.py"
    script_path.write_text(
        "from pathlib import Path\n"
        f"counter = Path({str(counter_path)!r})\n"
        "counter.write_text(str(int(counter.read_text() or '0') + 1) if counter.exists() else '1')\n"
        "raise SystemExit(2)\n",
        encoding="utf-8",
    )
    return script_path, counter_path


def _write_mcp_config(tmp_path: Path, server_script: Path) -> Path:
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "demo server": {
                        "type": "stdio",
                        "command": sys.executable,
                        "args": [str(server_script)],
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return config_path


def _write_multi_mcp_config(tmp_path: Path, servers: dict[str, Path]) -> Path:
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    server_name: {
                        "type": "stdio",
                        "command": sys.executable,
                        "args": [str(server_script)],
                    }
                    for server_name, server_script in servers.items()
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return config_path


def _write_named_mcp_config(tmp_path: Path, server_name: str, server_script: Path) -> Path:
    config_path = tmp_path / "mcp.json"
    config_path.write_text(
        json.dumps(
            {
                "mcpServers": {
                    server_name: {
                        "type": "stdio",
                        "command": sys.executable,
                        "args": [str(server_script)],
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return config_path


class McpRuntimeTest(unittest.TestCase):
    def tearDown(self):
        clear_mcp_cache()
        reset_mcp_manager()
        os.environ.pop("GA_MCP_CONFIG", None)
        os.environ.pop("GA_MCP_CALL_TIMEOUT", None)

    def test_build_mcp_tool_name_matches_claudecode_normalization(self):
        self.assertEqual(normalize_mcp_name("my server"), "my_server")
        self.assertEqual(normalize_mcp_name("search.web"), "search_web")
        self.assertEqual(
            build_mcp_tool_name("my server", "search.web"),
            "mcp__my_server__search_web",
        )

    def test_load_mcp_config_accepts_claudecode_mcpservers_shape(self):
        with _tempdir() as tmp:
            config_path = Path(tmp) / "mcp.json"
            config_path.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "fetch": {
                                "type": "stdio",
                                "command": "uvx",
                                "args": ["mcp-server-fetch"],
                            },
                            "exa": {"type": "sse", "url": "https://example.invalid/mcp"},
                        }
                    }
                ),
                encoding="utf-8",
            )

            config = load_mcp_config(config_path)

        self.assertEqual(set(config.servers), {"fetch", "exa"})
        self.assertEqual(config.servers["fetch"]["type"], "stdio")
        self.assertEqual(config.servers["exa"]["type"], "sse")

    def test_mcp_status_reports_configured_disabled_server_without_connecting(self):
        with _tempdir() as tmp:
            tmp_path = Path(tmp)
            server_script = _write_demo_server(tmp_path)
            config_path = tmp_path / "mcp.json"
            config_path.write_text(
                json.dumps(
                    {
                        "mcpServers": {
                            "demo": {
                                "type": "stdio",
                                "command": sys.executable,
                                "args": [str(server_script)],
                                "disabled": True,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            os.environ["GA_MCP_CONFIG"] = str(config_path)

            status = mcp_status(timeout=20)

        self.assertEqual(status["servers"][0]["name"], "demo")
        self.assertEqual(status["servers"][0]["status"], "disabled")
        self.assertEqual(status["servers"][0]["tool_count"], 0)

    def test_set_mcp_server_enabled_persists_disabled_flag(self):
        with _tempdir() as tmp:
            tmp_path = Path(tmp)
            server_script = _write_demo_server(tmp_path)
            config_path = _write_named_mcp_config(tmp_path, "demo", server_script)
            os.environ["GA_MCP_CONFIG"] = str(config_path)

            set_mcp_server_enabled("demo", False)
            disabled_data = json.loads(config_path.read_text(encoding="utf-8"))
            set_mcp_server_enabled("demo", True)
            enabled_data = json.loads(config_path.read_text(encoding="utf-8"))

        self.assertIs(disabled_data["mcpServers"]["demo"]["disabled"], True)
        self.assertNotIn("disabled", enabled_data["mcpServers"]["demo"])

    def test_get_mcp_manager_closes_previous_config_manager(self):
        with _tempdir() as tmp:
            tmp_path = Path(tmp)
            first_config = _write_named_mcp_config(tmp_path, "one", _write_demo_server(tmp_path))
            second_dir = tmp_path / "second"
            second_dir.mkdir()
            second_config = _write_named_mcp_config(second_dir, "two", _write_demo_server(second_dir))

            first_manager = get_mcp_manager(first_config)
            first_thread = first_manager.loop_thread
            get_mcp_manager(second_config)

        self.assertFalse(first_thread.is_alive())
        self.assertTrue(first_manager.loop.is_closed())

    def test_redacts_sensitive_values_from_mcp_errors(self):
        cases = [
            ("https://example.test/mcp?tavilyApiKey=abc123&x=1 token: xyz", ["abc123", "xyz"]),
            ("Authorization: Bearer sk-mcp-secret", ["sk-mcp-secret"]),
            ("Bearer bearer-mcp-secret", ["bearer-mcp-secret"]),
            ("x-api-key: xkey-mcp-secret", ["xkey-mcp-secret"]),
            ("provider key sk-ant-mcp-secret", ["sk-ant-mcp-secret"]),
            ("jwt=eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzZWNyZXQifQ.signature", ["eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJzZWNyZXQifQ.signature"]),
            ("https://example.test/mcp?token=tok-secret&access_token=access-secret&api_key=api-secret&secret=secret-value", ["tok-secret", "access-secret", "api-secret", "secret-value"]),
            ("Cookie: sid=cookie-secret; Set-Cookie: session=set-cookie-secret", ["cookie-secret", "set-cookie-secret"]),
        ]
        for text, leaked_values in cases:
            with self.subTest(text=text):
                msg = _redact_sensitive(text)
                self.assertIn("[REDACTED]", msg)
                for leaked in leaked_values:
                    self.assertNotIn(leaked, msg)

    def test_discover_and_call_stdio_fastmcp_tool(self):
        with _tempdir() as tmp:
            tmp_path = Path(tmp)
            config_path = _write_mcp_config(tmp_path, _write_demo_server(tmp_path))

            tools = discover_mcp_tools(config_path=config_path, timeout=20)
            schema_by_name = {tool["function"]["name"]: tool for tool in tools}

            self.assertIn("mcp__demo_server__echo", schema_by_name)
            echo_schema = schema_by_name["mcp__demo_server__echo"]["function"]
            self.assertIn("demo server/echo", echo_schema["description"])
            self.assertEqual(
                echo_schema["parameters"]["properties"]["text"]["type"],
                "string",
            )

            result = call_mcp_tool(
                "mcp__demo_server__echo",
                {"text": "hi"},
                config_path=config_path,
                timeout=20,
            )

        self.assertEqual(result["status"], "success")
        self.assertIn("echo:hi", json.dumps(result, ensure_ascii=False))

    def test_handler_dispatches_dynamic_mcp_tool(self):
        with _tempdir() as tmp:
            tmp_path = Path(tmp)
            config_path = _write_mcp_config(tmp_path, _write_demo_server(tmp_path))
            os.environ["GA_MCP_CONFIG"] = str(config_path)

            parent = type("Parent", (), {"task_dir": None, "verbose": False})()
            handler = GenericAgentHandler(parent, last_history=[], cwd=tmp)
            outcome = exhaust(
                handler.dispatch(
                    "mcp__demo_server__echo",
                    {"text": "dispatch"},
                    type("Response", (), {"content": ""})(),
                )
            )

        self.assertEqual(outcome.data["status"], "success")
        self.assertIn("echo:dispatch", json.dumps(outcome.data, ensure_ascii=False))

    def test_generic_agent_abort_stops_mcp_during_initial_connection(self):
        with _tempdir() as tmp:
            tmp_path = Path(tmp)
            server_script, started_marker, pid_path = _write_hanging_startup_server(tmp_path)
            config_path = _write_named_mcp_config(tmp_path, "starting", server_script)
            os.environ["GA_MCP_CONFIG"] = str(config_path)
            os.environ["GA_MCP_CALL_TIMEOUT"] = "2"
            reset_mcp_manager()

            agent = GenericAgent.__new__(GenericAgent)
            agent.is_running = True
            agent.stop_sig = False
            agent.llmclient = None
            handler = GenericAgentHandler(agent, last_history=[], cwd=tmp)
            agent.handler = handler
            box = {}

            def run_dispatch():
                box["outcome"] = exhaust(
                    handler.dispatch(
                        "mcp__starting__never",
                        {},
                        type("Response", (), {"content": ""})(),
                    )
                )

            worker = threading.Thread(target=run_dispatch, daemon=True)
            worker.start()
            deadline = time.monotonic() + 3
            while not started_marker.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(started_marker.exists(), "MCP startup fixture did not launch")
            server_pid = int(pid_path.read_text(encoding="utf-8"))
            self.assertTrue(_process_exists(server_pid))

            started = time.monotonic()
            agent.abort()
            worker.join(timeout=0.75)
            stopped_quickly = not worker.is_alive()
            elapsed = time.monotonic() - started

            worker.join(timeout=5)
            manager = get_mcp_manager()
            manager_thread = manager.loop_thread
            reset_mcp_manager()
            process_exited = _wait_for_process_exit(server_pid, timeout=3)

        self.assertTrue(stopped_quickly, f"abort remained blocked in MCP startup for {elapsed:.2f}s")
        self.assertFalse(worker.is_alive())
        self.assertEqual(box["outcome"].data["status"], "error")
        self.assertRegex(str(box["outcome"].data.get("msg", "")), r"(?i)abort|cancel|stop")
        self.assertTrue(process_exited, f"startup MCP process {server_pid} survived manager shutdown")
        self.assertFalse(manager_thread.is_alive())

    def test_cancelled_discovery_waiter_stops_without_failing_shared_startup(self):
        with _tempdir() as tmp:
            tmp_path = Path(tmp)
            server_script, started_marker, pid_path = _write_hanging_startup_server(tmp_path)
            config_path = _write_named_mcp_config(tmp_path, "starting", server_script)
            cache_path = tmp_path / "cancelled-discovery-cache.json"
            os.environ["GA_MCP_CONFIG"] = str(config_path)
            reset_mcp_manager()
            stop_signal = threading.Event()
            box = {}

            def run_discovery():
                try:
                    with mcp_cancellation_scope(stop_signal):
                        box["tools"] = discover_mcp_tools_cached(timeout=30, cache_path=cache_path)
                except BaseException as exc:  # pragma: no cover - asserted below
                    box["error"] = exc

            worker = threading.Thread(target=run_discovery, daemon=True)
            worker.start()
            deadline = time.monotonic() + 3
            while not started_marker.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(started_marker.exists(), "MCP discovery fixture did not launch")
            server_pid = int(pid_path.read_text(encoding="utf-8"))

            stop_signal.set()
            worker.join(timeout=0.75)
            stopped_quickly = not worker.is_alive()
            manager = get_mcp_manager()
            with manager.lock:
                server_status = manager.states["starting"].status
            cache_written = cache_path.exists()
            reset_mcp_manager()
            worker.join(timeout=3)
            process_exited = _wait_for_process_exit(server_pid, timeout=3)

        self.assertTrue(stopped_quickly, "MCP discovery ignored the turn cancellation signal")
        self.assertNotIn("error", box)
        self.assertEqual(box["tools"], [])
        self.assertFalse(cache_written, "cancelled discovery wrote an incomplete MCP tool cache")
        self.assertEqual(server_status, "pending")
        self.assertTrue(process_exited, f"startup MCP process {server_pid} survived manager shutdown")

    def test_cancelled_waiter_does_not_cancel_shared_server_startup(self):
        with _tempdir() as tmp:
            tmp_path = Path(tmp)
            server_script, starts_path = _write_delayed_counting_server(tmp_path)
            config_path = _write_named_mcp_config(tmp_path, "delayed", server_script)
            os.environ["GA_MCP_CONFIG"] = str(config_path)
            os.environ["GA_MCP_CALL_TIMEOUT"] = "10"
            reset_mcp_manager()

            agents = []
            workers = []
            boxes = [{}, {}]
            for index in range(2):
                agent = GenericAgent.__new__(GenericAgent)
                agent.is_running = True
                agent.stop_sig = False
                agent.llmclient = None
                handler = GenericAgentHandler(agent, last_history=[], cwd=tmp)
                agent.handler = handler
                agents.append(agent)

                def run_dispatch(slot=index, active_handler=handler):
                    boxes[slot]["outcome"] = exhaust(
                        active_handler.dispatch(
                            "mcp__delayed__echo",
                            {"text": f"call-{slot}"},
                            type("Response", (), {"content": ""})(),
                        )
                    )

                worker = threading.Thread(target=run_dispatch, daemon=True)
                workers.append(worker)
                worker.start()

            deadline = time.monotonic() + 3
            while not starts_path.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(starts_path.exists(), "shared startup fixture did not launch")
            time.sleep(0.2)

            agents[0].abort()
            workers[0].join(timeout=1)
            workers[1].join(timeout=10)
            starts = int(starts_path.read_text(encoding="utf-8"))

        self.assertFalse(workers[0].is_alive())
        self.assertFalse(workers[1].is_alive())
        self.assertRegex(str(boxes[0]["outcome"].data.get("msg", "")), r"(?i)abort|cancel|stop")
        self.assertEqual(boxes[1]["outcome"].data["status"], "success")
        self.assertEqual(starts, 1)

    def test_short_timeout_waiter_does_not_cancel_shared_server_startup(self):
        with _tempdir() as tmp:
            tmp_path = Path(tmp)
            server_script, starts_path = _write_delayed_counting_server(tmp_path, startup_delay=3.0)
            config_path = _write_named_mcp_config(tmp_path, "delayed", server_script)
            os.environ["GA_MCP_CONFIG"] = str(config_path)
            reset_mcp_manager()

            boxes = [{}, {}]

            def call_tool(slot, timeout):
                boxes[slot]["result"] = call_mcp_tool(
                    "mcp__delayed__echo",
                    {"text": f"call-{slot}"},
                    timeout=timeout,
                )

            short_waiter = threading.Thread(target=call_tool, args=(0, 1.5), daemon=True)
            short_waiter.start()
            deadline = time.monotonic() + 3
            while not starts_path.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(starts_path.exists(), "shared startup fixture did not launch")

            long_waiter = threading.Thread(target=call_tool, args=(1, 5), daemon=True)
            long_waiter.start()
            short_waiter.join(timeout=5)
            long_waiter.join(timeout=10)
            starts = int(starts_path.read_text(encoding="utf-8"))

        self.assertFalse(short_waiter.is_alive())
        self.assertFalse(long_waiter.is_alive())
        self.assertEqual(boxes[0]["result"]["status"], "error")
        self.assertRegex(str(boxes[0]["result"].get("msg", "")), r"(?i)timeout|timed out")
        self.assertEqual(boxes[1]["result"]["status"], "success")
        self.assertIn("echo:call-1", json.dumps(boxes[1]["result"], ensure_ascii=False))
        self.assertEqual(starts, 1)

    def test_concurrent_discovery_reuses_shared_server_startup(self):
        with _tempdir() as tmp:
            tmp_path = Path(tmp)
            server_script, starts_path = _write_delayed_counting_server(tmp_path, startup_delay=1.0)
            config_path = _write_named_mcp_config(tmp_path, "delayed", server_script)
            os.environ["GA_MCP_CONFIG"] = str(config_path)
            reset_mcp_manager()
            boxes = [{}, {}]

            def discover(slot):
                boxes[slot]["tools"] = discover_mcp_tools(timeout=5)

            workers = [threading.Thread(target=discover, args=(slot,), daemon=True) for slot in range(2)]
            for worker in workers:
                worker.start()
            for worker in workers:
                worker.join(timeout=8)
            starts = int(starts_path.read_text(encoding="utf-8"))

        self.assertTrue(all(not worker.is_alive() for worker in workers))
        for box in boxes:
            self.assertIn("mcp__delayed__echo", {tool["function"]["name"] for tool in box["tools"]})
        self.assertEqual(starts, 1)


    def test_call_mcp_tool_timeout_returns_error_without_hanging(self):
        with _tempdir() as tmp:
            tmp_path = Path(tmp)
            server_script, starts_path, pid_path = _write_reconnect_after_timeout_server(tmp_path)
            config_path = _write_named_mcp_config(tmp_path, "timeout", server_script)
            os.environ["GA_MCP_CONFIG"] = str(config_path)
            reset_mcp_manager()

            warmup = call_mcp_tool(
                "mcp__timeout__delay",
                {"text": "warmup", "seconds": 0},
                timeout=10,
            )
            self.assertEqual(warmup["status"], "success")
            first_pid = int(pid_path.read_text(encoding="utf-8"))

            started = time.monotonic()
            result = call_mcp_tool(
                "mcp__timeout__delay",
                {"text": "slow", "seconds": 30},
                timeout=0.5,
            )
            elapsed = time.monotonic() - started
            first_process_exited = _wait_for_process_exit(first_pid, timeout=3)
            manager = get_mcp_manager()
            with manager.lock:
                status_after_timeout = manager.states["timeout"].status
            pending_after_timeout = manager._run(_pending_manager_tasks(), timeout=2)

            restored = call_mcp_tool(
                "mcp__timeout__delay",
                {"text": "fast", "seconds": 0},
                timeout=5,
            )
            starts = int(starts_path.read_text(encoding="utf-8"))

        self.assertEqual(result["status"], "error")
        self.assertRegex(str(result.get("msg", "")), r"(?i)timeout|timed out")
        self.assertLess(elapsed, 5.0)
        self.assertTrue(first_process_exited, f"timed-out MCP process {first_pid} survived")
        self.assertEqual(status_after_timeout, "pending")
        self.assertEqual(pending_after_timeout, [])
        self.assertEqual(restored["status"], "success")
        self.assertIn("done:fast", json.dumps(restored, ensure_ascii=False))
        self.assertEqual(starts, 2)

    def test_generic_agent_abort_during_mcp_call_does_not_fail_server(self):
        with _tempdir() as tmp:
            tmp_path = Path(tmp)
            server_script, call_marker, pid_path = _write_cancellable_server(tmp_path)
            config_path = _write_named_mcp_config(tmp_path, "cancellable", server_script)
            os.environ["GA_MCP_CONFIG"] = str(config_path)
            reset_mcp_manager()

            agent = GenericAgent.__new__(GenericAgent)
            agent.is_running = True
            agent.stop_sig = False
            agent.llmclient = None
            handler = GenericAgentHandler(agent, last_history=[], cwd=tmp)
            agent.handler = handler
            box = {}

            def run_dispatch():
                try:
                    box["outcome"] = exhaust(
                        handler.dispatch(
                            "mcp__cancellable__hang",
                            {"text": "stop-me"},
                            type("Response", (), {"content": ""})(),
                        )
                    )
                except Exception as exc:  # pragma: no cover - asserted below
                    box["error"] = exc

            worker = threading.Thread(target=run_dispatch, daemon=True)
            worker.start()
            deadline = time.monotonic() + 5
            while not call_marker.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(call_marker.exists(), "MCP tool fixture did not start")
            server_pid = int(pid_path.read_text(encoding="utf-8"))
            self.assertTrue(_process_exists(server_pid))

            agent.abort()
            worker.join(timeout=5)
            process_exited = _wait_for_process_exit(server_pid, timeout=3)
            manager = get_mcp_manager()
            with manager.lock:
                server_status = manager.states["cancellable"].status
            pending_after_cancel = manager._run(_pending_manager_tasks(), timeout=2)

        self.assertFalse(worker.is_alive())
        self.assertNotIn("error", box)
        outcome = box["outcome"]
        self.assertEqual(outcome.data["status"], "error")
        self.assertRegex(str(outcome.data.get("msg", "")), r"(?i)abort|stop|cancel")
        self.assertTrue(process_exited, f"MCP stdio process {server_pid} survived cancellation")
        self.assertEqual(server_status, "pending")
        self.assertEqual(pending_after_cancel, [])

    def test_manager_shutdown_cancels_inflight_call_and_stops_server(self):
        with _tempdir() as tmp:
            tmp_path = Path(tmp)
            server_script, call_marker, pid_path = _write_cancellable_server(tmp_path)
            config_path = _write_named_mcp_config(tmp_path, "cancellable", server_script)
            os.environ["GA_MCP_CONFIG"] = str(config_path)
            reset_mcp_manager()
            box = {}

            def run_call():
                try:
                    box["result"] = call_mcp_tool(
                        "mcp__cancellable__hang",
                        {"text": "shutdown"},
                        timeout=60,
                    )
                except BaseException as exc:  # pragma: no cover - asserted below
                    box["error"] = exc

            worker = threading.Thread(target=run_call, daemon=True)
            worker.start()
            deadline = time.monotonic() + 5
            while not call_marker.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
            self.assertTrue(call_marker.exists(), "MCP tool fixture did not start")
            server_pid = int(pid_path.read_text(encoding="utf-8"))
            self.assertTrue(_process_exists(server_pid))
            manager_thread = get_mcp_manager().loop_thread

            reset_mcp_manager()
            worker.join(timeout=5)
            process_exited = _wait_for_process_exit(server_pid, timeout=3)

        self.assertFalse(worker.is_alive(), "in-flight MCP call survived manager shutdown")
        self.assertNotIn("error", box)
        self.assertEqual(box["result"]["status"], "error")
        self.assertRegex(str(box["result"].get("msg", "")), r"(?i)abort|cancel|closed|shutdown")
        self.assertTrue(process_exited, f"MCP stdio process {server_pid} survived manager shutdown")
        self.assertFalse(manager_thread.is_alive())

    def test_manager_reuses_stdio_server_across_calls(self):
        with _tempdir() as tmp:
            tmp_path = Path(tmp)
            server_script = _write_counting_server(tmp_path)
            config_path = _write_named_mcp_config(tmp_path, "counting", server_script)
            os.environ["GA_MCP_CONFIG"] = str(config_path)
            reset_mcp_manager()

            first = call_mcp_tool("mcp__counting__echo", {"text": "one"}, timeout=20)
            second = call_mcp_tool("mcp__counting__echo", {"text": "two"}, timeout=20)
            starts = int((tmp_path / "starts.txt").read_text(encoding="utf-8"))

        self.assertEqual(first["status"], "success")
        self.assertEqual(second["status"], "success")
        self.assertEqual(starts, 1)

    def test_cached_discovery_reuses_tool_schema_without_restarting_server(self):
        with _tempdir() as tmp:
            tmp_path = Path(tmp)
            server_script = _write_counting_server(tmp_path)
            cache_path = tmp_path / "mcp_tools_cache.json"
            config_path = _write_named_mcp_config(tmp_path, "counting", server_script)
            reset_mcp_manager()

            first_tools = discover_mcp_tools_cached(
                config_path=config_path,
                timeout=20,
                cache_path=cache_path,
            )
            reset_mcp_manager()
            second_tools = discover_mcp_tools_cached(
                config_path=config_path,
                timeout=20,
                cache_path=cache_path,
            )
            starts = int((tmp_path / "starts.txt").read_text(encoding="utf-8"))

        self.assertEqual(
            {tool["function"]["name"] for tool in first_tools},
            {tool["function"]["name"] for tool in second_tools},
        )
        self.assertIn("mcp__counting__echo", {tool["function"]["name"] for tool in second_tools})
        self.assertEqual(starts, 1)

    def test_incomplete_discovery_cache_marks_complete_false_and_expires_by_ttl(self):
        # Root cause of the "tavily missing" bug: when one server times out at
        # discovery, the partial tool set was cached permanently (signature only
        # tracked config file + server names, never which servers connected).
        # A transient remote timeout then hid that server's tools forever.
        # Fix: partial results are marked complete=false and expire by a short TTL.
        import mcp_runtime

        with _tempdir() as tmp:
            tmp_path = Path(tmp)
            good_script = _write_demo_server(tmp_path)
            bad_script, _bad_marker = _write_failing_marker_server(tmp_path)
            cache_path = tmp_path / "mcp_tools_cache.json"
            config_path = _write_multi_mcp_config(
                tmp_path,
                {"good": good_script, "bad": bad_script},
            )
            reset_mcp_manager()

            first_tools = discover_mcp_tools_cached(
                config_path=config_path,
                timeout=20,
                cache_path=cache_path,
            )
            first_names = {tool["function"]["name"] for tool in first_tools}
            # The good server still surfaces its tool despite the bad server failing.
            self.assertIn("mcp__good__echo", first_names)

            cache_data = json.loads(cache_path.read_text(encoding="utf-8"))
            # A failed server means the tool set is partial → complete must be false.
            self.assertFalse(cache_data.get("complete", True))

            # Within TTL the partial cache is reused as-is.
            reset_mcp_manager()
            cached_again = discover_mcp_tools_cached(
                config_path=config_path,
                timeout=20,
                cache_path=cache_path,
            )
            self.assertEqual(
                first_names,
                {tool["function"]["name"] for tool in cached_again},
            )

            # Age the partial cache past its TTL: it must NOT be reused, so a
            # recovered server's tools can reappear instead of staying hidden.
            stale = json.loads(cache_path.read_text(encoding="utf-8"))
            stale["cached_at"] = time.time() - (mcp_runtime._MCP_TOOLS_CACHE_INCOMPLETE_TTL + 10)
            cache_path.write_text(json.dumps(stale, ensure_ascii=False), encoding="utf-8")

            self.assertIsNone(
                mcp_runtime._read_mcp_tools_cache(
                    cache_path,
                    mcp_runtime._cache_signature(
                        mcp_runtime.load_mcp_config_with_disabled(config_path),
                        False,
                    ),
                ),
            )

    def test_complete_discovery_cache_has_no_ttl_expiry(self):
        # Complete results (every server connected) stay cached as long as the
        # config signature matches — no TTL — to avoid paying discovery cost each
        # turn. Only partial results self-heal via TTL.
        import mcp_runtime

        with _tempdir() as tmp:
            tmp_path = Path(tmp)
            server_script = _write_counting_server(tmp_path)
            cache_path = tmp_path / "mcp_tools_cache.json"
            config_path = _write_named_mcp_config(tmp_path, "counting", server_script)
            reset_mcp_manager()

            discover_mcp_tools_cached(
                config_path=config_path,
                timeout=20,
                cache_path=cache_path,
            )
            cache_data = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertTrue(cache_data.get("complete"))

            # Even aged far past the incomplete TTL, a complete cache is still valid.
            aged = json.loads(cache_path.read_text(encoding="utf-8"))
            aged["cached_at"] = time.time() - (mcp_runtime._MCP_TOOLS_CACHE_INCOMPLETE_TTL * 100)
            cache_path.write_text(json.dumps(aged, ensure_ascii=False), encoding="utf-8")

            reused = mcp_runtime._read_mcp_tools_cache(
                cache_path,
                mcp_runtime._cache_signature(
                    mcp_runtime.load_mcp_config_with_disabled(config_path),
                    False,
                ),
            )
            self.assertIsNotNone(reused)
            self.assertIn(
                "mcp__counting__echo",
                {tool["function"]["name"] for tool in reused},
            )

    def test_call_tool_only_connects_target_server(self):
        with _tempdir() as tmp:
            tmp_path = Path(tmp)
            bad_script, bad_marker = _write_failing_marker_server(tmp_path)
            config_path = _write_multi_mcp_config(
                tmp_path,
                {
                    "demo": _write_demo_server(tmp_path),
                    "bad": bad_script,
                },
            )
            os.environ["GA_MCP_CONFIG"] = str(config_path)
            reset_mcp_manager()

            result = call_mcp_tool("mcp__demo__echo", {"text": "target"}, timeout=20)
            manager = get_mcp_manager()
            with manager.lock:
                statuses = {name: state.status for name, state in manager.states.items()}

            self.assertEqual(result["status"], "success")
            self.assertIn("echo:target", json.dumps(result, ensure_ascii=False))
            self.assertFalse(bad_marker.exists())
            self.assertEqual(statuses, {"demo": "connected", "bad": "pending"})

    def test_discover_does_not_retry_failed_servers_until_explicit_status_or_reconnect(self):
        with _tempdir() as tmp:
            tmp_path = Path(tmp)
            bad_script, bad_counter = _write_failing_counting_server(tmp_path)
            config_path = _write_multi_mcp_config(
                tmp_path,
                {
                    "demo": _write_demo_server(tmp_path),
                    "bad": bad_script,
                },
            )
            os.environ["GA_MCP_CONFIG"] = str(config_path)
            reset_mcp_manager()

            first_tools = discover_mcp_tools(timeout=20)
            second_tools = discover_mcp_tools(timeout=20)
            starts_after_discover = int(bad_counter.read_text(encoding="utf-8"))
            mcp_status(timeout=20)
            starts_after_status = int(bad_counter.read_text(encoding="utf-8"))

        self.assertIn("mcp__demo__echo", {tool["function"]["name"] for tool in first_tools})
        self.assertIn("mcp__demo__echo", {tool["function"]["name"] for tool in second_tools})
        self.assertEqual(starts_after_discover, 1)
        self.assertEqual(starts_after_status, 2)

    def test_unknown_mcp_server_does_not_connect_any_server(self):
        with _tempdir() as tmp:
            tmp_path = Path(tmp)
            bad_script, bad_marker = _write_failing_marker_server(tmp_path)
            config_path = _write_multi_mcp_config(
                tmp_path,
                {
                    "demo": _write_demo_server(tmp_path),
                    "bad": bad_script,
                },
            )
            os.environ["GA_MCP_CONFIG"] = str(config_path)
            reset_mcp_manager()

            result = call_mcp_tool("mcp__dmeo__echo", {"text": "target"}, timeout=20)
            manager = get_mcp_manager()
            with manager.lock:
                statuses = {name: state.status for name, state in manager.states.items()}

            self.assertEqual(result["status"], "error")
            self.assertIn("Unknown MCP server", result["msg"])
            self.assertIn("demo", result["msg"])
            self.assertFalse(bad_marker.exists())
            self.assertEqual(statuses, {"demo": "pending", "bad": "pending"})

    def test_mcp_tool_description_is_truncated_for_schema_stability(self):
        with _tempdir() as tmp:
            tmp_path = Path(tmp)
            config_path = _write_named_mcp_config(tmp_path, "longdesc", _write_long_description_server(tmp_path))

            tools = discover_mcp_tools(config_path=config_path, timeout=20)

        echo_schema = next(tool for tool in tools if tool["function"]["name"] == "mcp__longdesc__echo")["function"]
        self.assertIn("[MCP: longdesc/echo]", echo_schema["description"])
        self.assertIn("[truncated]", echo_schema["description"])
        self.assertLessEqual(len(echo_schema["description"]), 2100)
        self.assertNotIn("A" * 2500, echo_schema["description"])

    def test_reconnect_restarts_stdio_server(self):
        with _tempdir() as tmp:
            tmp_path = Path(tmp)
            server_script = _write_counting_server(tmp_path)
            config_path = _write_named_mcp_config(tmp_path, "counting", server_script)
            os.environ["GA_MCP_CONFIG"] = str(config_path)
            reset_mcp_manager()

            call_mcp_tool("mcp__counting__echo", {"text": "one"}, timeout=20)
            reconnect = reconnect_mcp_server("counting", timeout=20)
            call_mcp_tool("mcp__counting__echo", {"text": "two"}, timeout=20)
            starts = int((tmp_path / "starts.txt").read_text(encoding="utf-8"))

        self.assertEqual(reconnect["server"]["status"], "connected")
        self.assertEqual(starts, 2)

    def test_disable_and_enable_mcp_server_update_status_and_tools(self):
        with _tempdir() as tmp:
            tmp_path = Path(tmp)
            server_script = _write_counting_server(tmp_path)
            config_path = _write_named_mcp_config(tmp_path, "counting", server_script)
            os.environ["GA_MCP_CONFIG"] = str(config_path)
            reset_mcp_manager()

            call_mcp_tool("mcp__counting__echo", {"text": "one"}, timeout=20)
            disabled = disable_mcp_server("counting")
            disabled_data = json.loads(config_path.read_text(encoding="utf-8"))
            unavailable = call_mcp_tool("mcp__counting__echo", {"text": "blocked"}, timeout=20)
            enabled = enable_mcp_server("counting", timeout=20)
            enabled_data = json.loads(config_path.read_text(encoding="utf-8"))
            restored = call_mcp_tool("mcp__counting__echo", {"text": "two"}, timeout=20)

        self.assertEqual(disabled["server"]["status"], "disabled")
        self.assertEqual(disabled["server"]["tool_count"], 0)
        self.assertIs(disabled_data["mcpServers"]["counting"]["disabled"], True)
        self.assertEqual(unavailable["status"], "error")
        self.assertEqual(enabled["server"]["status"], "connected")
        self.assertNotIn("disabled", enabled_data["mcpServers"]["counting"])
        self.assertEqual(restored["status"], "success")

    def test_stdio_server_stderr_is_utf8_log_file_not_console_output(self):
        with _tempdir() as tmp:
            tmp_path = Path(tmp)
            config_path = _write_named_mcp_config(tmp_path, "zh server", _write_chinese_stderr_server(tmp_path))

            tools = discover_mcp_tools(config_path=config_path, timeout=20)
            self.assertIn("mcp__zh_server__hello", {tool["function"]["name"] for tool in tools})

            result = call_mcp_tool(
                "mcp__zh_server__hello",
                {},
                config_path=config_path,
                timeout=20,
            )

        self.assertEqual(result["status"], "success")
        self.assertIn("中文结果", json.dumps(result, ensure_ascii=False))
        log_text = (_MCP_LOG_DIR / "zh_server.stderr.log").read_text(encoding="utf-8", errors="replace")
        self.assertIn("中文stderr启动日志", log_text)
        self.assertIn("中文stderr调用日志", log_text)
        self.assertNotIn("ä¸­", log_text)


if __name__ == "__main__":
    unittest.main()
