import json
import os
import shutil
import sys
import unittest
import uuid
from contextlib import contextmanager
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from agent_loop import exhaust  # noqa: E402
from ga import GenericAgentHandler  # noqa: E402
from mcp_runtime import (  # noqa: E402
    build_mcp_tool_name,
    call_mcp_tool,
    clear_mcp_cache,
    discover_mcp_tools,
    disable_mcp_server,
    enable_mcp_server,
    get_mcp_manager,
    load_mcp_config,
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
        msg = _redact_sensitive("https://example.test/mcp?tavilyApiKey=abc123&x=1 token: xyz")

        self.assertIn("tavilyApiKey=[REDACTED]", msg)
        self.assertIn("token=[REDACTED]", msg)
        self.assertNotIn("abc123", msg)
        self.assertNotIn("xyz", msg)

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
