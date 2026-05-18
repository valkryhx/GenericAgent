import asyncio
import json
import os
import re
import threading
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional


script_dir = os.path.dirname(os.path.abspath(__file__))
MCP_TOOL_PREFIX = "mcp__"
_MCP_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]")
_DISCOVERY_CACHE: dict[tuple, "McpDiscovery"] = {}
_MCP_LOG_DIR = Path(script_dir) / "temp" / "mcp_logs"


@dataclass(frozen=True)
class McpConfig:
    path: Optional[Path]
    servers: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class McpToolRef:
    full_name: str
    server_name: str
    tool_name: str
    server_config: dict[str, Any]
    schema: dict[str, Any]


@dataclass
class McpDiscovery:
    tools: list[dict[str, Any]] = field(default_factory=list)
    tool_refs: dict[str, McpToolRef] = field(default_factory=dict)
    errors: dict[str, str] = field(default_factory=dict)
    discovered_at: float = field(default_factory=time.time)


@dataclass
class McpServerState:
    name: str
    config: dict[str, Any]
    status: str = "pending"
    error: str = ""
    tools: list[dict[str, Any]] = field(default_factory=list)
    tool_refs: dict[str, McpToolRef] = field(default_factory=dict)
    client: Any = None
    entered: Any = None


_MANAGER_LOCK = threading.Lock()
_MANAGER: Optional["McpManager"] = None


def normalize_mcp_name(name: str) -> str:
    return _MCP_NAME_RE.sub("_", str(name))


def build_mcp_tool_name(server_name: str, tool_name: str) -> str:
    return f"{MCP_TOOL_PREFIX}{normalize_mcp_name(server_name)}__{normalize_mcp_name(tool_name)}"


def clear_mcp_cache() -> None:
    _DISCOVERY_CACHE.clear()


def default_mcp_config_path() -> Path:
    return Path(os.environ.get("GA_MCP_CONFIG") or Path(script_dir) / "mcp.json")


def load_mcp_config(config_path: Optional[os.PathLike | str] = None) -> McpConfig:
    path = Path(config_path) if config_path is not None else default_mcp_config_path()
    if not path.is_file():
        return McpConfig(path=path, servers={})
    data = json.loads(path.read_text(encoding="utf-8"))
    raw_servers = data.get("mcpServers", data if isinstance(data, dict) else {})
    if not isinstance(raw_servers, dict):
        raise ValueError("mcp.json must contain an object field named mcpServers")
    servers = {
        str(name): dict(config)
        for name, config in raw_servers.items()
        if isinstance(config, dict) and not config.get("disabled")
    }
    return McpConfig(path=path, servers=servers)


def load_mcp_config_with_disabled(config_path: Optional[os.PathLike | str] = None) -> McpConfig:
    path = Path(config_path) if config_path is not None else default_mcp_config_path()
    if not path.is_file():
        return McpConfig(path=path, servers={})
    data = json.loads(path.read_text(encoding="utf-8"))
    raw_servers = data.get("mcpServers", data if isinstance(data, dict) else {})
    if not isinstance(raw_servers, dict):
        raise ValueError("mcp.json must contain an object field named mcpServers")
    servers = {
        str(name): dict(config)
        for name, config in raw_servers.items()
        if isinstance(config, dict)
    }
    return McpConfig(path=path, servers=servers)


class McpManager:
    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.lock = threading.RLock()
        self.states: dict[str, McpServerState] = {}
        self.reload_config()

    def reload_config(self) -> None:
        cfg = load_mcp_config_with_disabled(self.config_path)
        with self.lock:
            for state in self.states.values():
                self._close_state(state)
            self.states = {}
            for name, server_config in cfg.servers.items():
                status = "disabled" if server_config.get("disabled") else "pending"
                self.states[name] = McpServerState(
                    name=name,
                    config=dict(server_config),
                    status=status,
                )

    def status(self, timeout: Optional[float] = None) -> dict[str, Any]:
        del timeout
        with self.lock:
            servers = [self._server_summary(state) for state in self.states.values()]
            tools = [dict(tool) for state in self.states.values() for tool in state.tools]
            errors = {state.name: state.error for state in self.states.values() if state.error}
        return {
            "config_path": str(self.config_path),
            "servers": servers,
            "tools": tools,
            "errors": errors,
        }

    def close(self) -> None:
        with self.lock:
            for state in self.states.values():
                self._close_state(state)

    def _close_state(self, state: McpServerState) -> None:
        state.client = None
        state.entered = None

    def _server_summary(self, state: McpServerState) -> dict[str, Any]:
        transport = state.config.get("type") or state.config.get("transport")
        if not transport:
            transport = "stdio" if state.config.get("command") else "unknown"
        return {
            "name": state.name,
            "status": state.status,
            "transport": str(transport),
            "disabled": state.status == "disabled",
            "error": state.error,
            "tool_count": len(state.tools),
        }


def get_mcp_manager(config_path: Optional[os.PathLike | str] = None) -> McpManager:
    global _MANAGER
    path = Path(config_path) if config_path is not None else default_mcp_config_path()
    with _MANAGER_LOCK:
        if _MANAGER is None or _MANAGER.config_path != path:
            _MANAGER = McpManager(path)
        return _MANAGER


def reset_mcp_manager() -> None:
    global _MANAGER
    with _MANAGER_LOCK:
        manager = _MANAGER
        _MANAGER = None
    if manager is not None:
        manager.close()


def mcp_status(
    config_path: Optional[os.PathLike | str] = None,
    timeout: Optional[float] = None,
) -> dict[str, Any]:
    return get_mcp_manager(config_path).status(timeout=timeout)


def set_mcp_server_enabled(
    server_name: str,
    enabled: bool,
    config_path: Optional[os.PathLike | str] = None,
) -> None:
    path = Path(config_path) if config_path is not None else default_mcp_config_path()
    if not path.is_file():
        raise FileNotFoundError(str(path))
    data = json.loads(path.read_text(encoding="utf-8"))
    raw_servers = data.get("mcpServers", data if isinstance(data, dict) else {})
    if not isinstance(raw_servers, dict) or not isinstance(raw_servers.get(server_name), dict):
        raise KeyError(f"Unknown MCP server: {server_name}")
    if enabled:
        raw_servers[server_name].pop("disabled", None)
    else:
        raw_servers[server_name]["disabled"] = True
    path.write_text(json.dumps(data, ensure_ascii=False, indent=4), encoding="utf-8")
    clear_mcp_cache()
    get_mcp_manager(path).reload_config()


def discover_mcp_tools(
    config_path: Optional[os.PathLike | str] = None,
    include_unavailable: bool = False,
    timeout: Optional[float] = None,
) -> list[dict[str, Any]]:
    discovery = discover_mcp(config_path=config_path, include_unavailable=include_unavailable, timeout=timeout)
    return [dict(tool) for tool in discovery.tools]


def discover_mcp(
    config_path: Optional[os.PathLike | str] = None,
    include_unavailable: bool = False,
    timeout: Optional[float] = None,
) -> McpDiscovery:
    timeout = _default_timeout(timeout)
    cfg = load_mcp_config(config_path)
    if not cfg.servers:
        return McpDiscovery()

    signature = _config_signature(cfg, include_unavailable, timeout)
    ttl = float(os.environ.get("GA_MCP_CACHE_TTL", "30"))
    cached = _DISCOVERY_CACHE.get(signature)
    if cached is not None and time.time() - cached.discovered_at <= ttl:
        return cached

    discovery = _run_async(_discover_mcp_async(cfg, include_unavailable=include_unavailable, timeout=timeout))
    _DISCOVERY_CACHE[signature] = discovery
    return discovery


def call_mcp_tool(
    full_name: str,
    arguments: Optional[dict[str, Any]] = None,
    config_path: Optional[os.PathLike | str] = None,
    timeout: Optional[float] = None,
) -> dict[str, Any]:
    call_timeout = _default_timeout(timeout, env_name="GA_MCP_CALL_TIMEOUT", fallback=60)
    discovery = discover_mcp(config_path=config_path)
    tool_ref = discovery.tool_refs.get(full_name)
    if tool_ref is None:
        known = ", ".join(sorted(discovery.tool_refs)[:30])
        return {
            "status": "error",
            "msg": f"Unknown MCP tool: {full_name}" + (f". Known: {known}" if known else ""),
            "discovery_errors": discovery.errors,
        }
    clean_args = {k: v for k, v in (arguments or {}).items() if not str(k).startswith("_")}
    return _run_async(_call_mcp_tool_async(tool_ref, clean_args, timeout=call_timeout))


def _default_timeout(value: Optional[float], env_name: str = "GA_MCP_DISCOVERY_TIMEOUT", fallback: float = 8) -> float:
    if value is not None:
        return float(value)
    try:
        return float(os.environ.get(env_name, fallback))
    except (TypeError, ValueError):
        return fallback


def _config_signature(cfg: McpConfig, include_unavailable: bool, timeout: float) -> tuple:
    file_sig = None
    if cfg.path:
        try:
            stat = cfg.path.stat()
            file_sig = (str(cfg.path.resolve(strict=False)), stat.st_mtime_ns, stat.st_size)
        except OSError:
            file_sig = (str(cfg.path), None, None)
    return (file_sig, tuple(sorted(cfg.servers)), include_unavailable, timeout)


async def _discover_mcp_async(cfg: McpConfig, include_unavailable: bool, timeout: float) -> McpDiscovery:
    tasks = [
        _discover_server_tools(name, server_config, timeout=timeout)
        for name, server_config in cfg.servers.items()
    ]
    results = await asyncio.gather(*tasks)
    discovery = McpDiscovery()
    seen: set[str] = set()
    for server_name, server_config, tools, error in results:
        if error:
            discovery.errors[server_name] = error
            if not include_unavailable:
                continue
        for tool in tools:
            original_name = str(getattr(tool, "name", ""))
            if not original_name:
                continue
            full_name = build_mcp_tool_name(server_name, original_name)
            if full_name in seen:
                discovery.errors[server_name] = f"Duplicate normalized MCP tool name skipped: {full_name}"
                continue
            seen.add(full_name)
            schema = _tool_to_function_schema(server_name, tool, full_name)
            discovery.tools.append(schema)
            discovery.tool_refs[full_name] = McpToolRef(
                full_name=full_name,
                server_name=server_name,
                tool_name=original_name,
                server_config=dict(server_config),
                schema=schema,
            )
    return discovery


async def _discover_server_tools(server_name: str, server_config: dict[str, Any], timeout: float):
    try:
        single_config = _single_server_config(server_name, server_config)
        async def _list():
            async with _mcp_client(single_config, server_name, timeout=timeout) as client:
                return await client.list_tools()

        tools = await asyncio.wait_for(_list(), timeout=timeout)
        return server_name, server_config, tools, None
    except Exception as e:
        return server_name, server_config, [], _redact_sensitive(f"{type(e).__name__}: {e}")


async def _call_mcp_tool_async(tool_ref: McpToolRef, arguments: dict[str, Any], timeout: float) -> dict[str, Any]:
    try:
        single_config = _single_server_config(tool_ref.server_name, tool_ref.server_config)
        async with _mcp_client(single_config, tool_ref.server_name, timeout=timeout) as client:
            result = await asyncio.wait_for(
                client.call_tool(tool_ref.tool_name, arguments, timeout=timeout, raise_on_error=False),
                timeout=timeout,
            )
        return _serialize_call_result(result)
    except Exception as e:
        return {"status": "error", "msg": _redact_sensitive(f"{type(e).__name__}: {e}")}


def _single_server_config(server_name: str, server_config: dict[str, Any]) -> dict[str, Any]:
    return {"mcpServers": {server_name: _normalize_server_config(server_config)}}


@asynccontextmanager
async def _mcp_client(single_config: dict[str, Any], server_name: str, timeout: float):
    from fastmcp import Client
    from fastmcp.client.transports import MCPConfigTransport, StdioTransport

    transport = MCPConfigTransport(single_config)
    stderr_log = None
    if isinstance(getattr(transport, "transport", None), StdioTransport):
        transport.transport.keep_alive = False
        _MCP_LOG_DIR.mkdir(parents=True, exist_ok=True)
        log_name = f"{normalize_mcp_name(server_name)}.stderr.log"
        stderr_log = (_MCP_LOG_DIR / log_name).open("a", encoding="utf-8", errors="replace")
        transport.transport.log_file = stderr_log
    try:
        async with Client(transport, name=f"ga-mcp-{server_name}", timeout=timeout, init_timeout=timeout) as client:
            yield client
    finally:
        if stderr_log is not None:
            stderr_log.close()


def _normalize_server_config(server_config: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(server_config)
    cfg_type = cfg.get("type")
    if cfg.get("url") and cfg_type in {"http", "streamable-http", "sse"} and not cfg.get("transport"):
        cfg["transport"] = cfg_type
    if cfg.get("command") and not cfg.get("transport"):
        cfg["transport"] = "stdio"
    if cfg.get("command"):
        merged_env = dict(os.environ)
        merged_env.update({str(k): str(v) for k, v in (cfg.get("env") or {}).items() if v is not None})
        merged_env.setdefault("PYTHONIOENCODING", "utf-8")
        merged_env.setdefault("PYTHONUTF8", "1")
        merged_env.setdefault("LC_ALL", "C.UTF-8")
        merged_env.setdefault("LANG", "C.UTF-8")
        cfg["env"] = merged_env
    return cfg


def _tool_to_function_schema(server_name: str, tool: Any, full_name: str) -> dict[str, Any]:
    tool_name = str(getattr(tool, "name", ""))
    description = str(getattr(tool, "description", "") or "").strip()
    description = f"[MCP: {server_name}/{tool_name}] {description}".strip()
    parameters = getattr(tool, "inputSchema", None) or {"type": "object", "properties": {}}
    if not isinstance(parameters, dict):
        parameters = {"type": "object", "properties": {}}
    parameters = _json_safe(parameters)
    if parameters.get("type") != "object":
        parameters = {"type": "object", "properties": {}, "x-original-schema": parameters}
    parameters.setdefault("properties", {})
    return {
        "type": "function",
        "function": {
            "name": full_name,
            "description": description,
            "parameters": parameters,
        },
    }


def _serialize_call_result(result: Any) -> dict[str, Any]:
    data = _json_safe(result)
    is_error = bool(getattr(result, "isError", False) or getattr(result, "is_error", False))
    payload: dict[str, Any] = {"status": "error" if is_error else "success", "result": data}

    content = getattr(result, "content", None)
    if content is not None:
        safe_content = _json_safe(content)
        payload["content"] = safe_content
        texts = []
        for item in content:
            text = getattr(item, "text", None)
            if text is not None:
                texts.append(str(text))
        if texts:
            payload["text"] = "\n".join(texts)

    structured = getattr(result, "structuredContent", None)
    if structured is None:
        structured = getattr(result, "structured_content", None)
    if structured is not None:
        payload["structured_content"] = _json_safe(structured)

    if hasattr(result, "data"):
        payload["data"] = _json_safe(getattr(result, "data"))
    return payload


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(v) for v in value]
    if hasattr(value, "model_dump"):
        try:
            return _json_safe(value.model_dump(by_alias=True, mode="json", exclude_none=True))
        except TypeError:
            return _json_safe(value.model_dump())
    return str(value)


def _redact_sensitive(text: str) -> str:
    text = str(text)
    text = re.sub(
        r"(?i)([?&][^=\s&]*(?:api[_-]?key|token|secret|password|apikey)[^=\s&]*=)[^&\s]+",
        r"\1[REDACTED]",
        text,
    )
    text = re.sub(
        r"(?i)\b(api[_-]?key|token|secret|password|apikey)\s*[:=]\s*['\"]?[^'\"\s,;}]+",
        r"\1=[REDACTED]",
        text,
    )
    return text


def _run_async(coro):
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    box: dict[str, Any] = {}

    def runner():
        try:
            box["result"] = asyncio.run(coro)
        except BaseException as e:
            box["error"] = e

    thread = threading.Thread(target=runner, daemon=True)
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box.get("result")
