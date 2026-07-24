import asyncio
import concurrent.futures
import json
import os
import re
import threading
import time
from contextlib import asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from sensitive_redaction import redact_sensitive_text


script_dir = os.path.dirname(os.path.abspath(__file__))
MCP_TOOL_PREFIX = "mcp__"
_MCP_NAME_RE = re.compile(r"[^a-zA-Z0-9_-]")
_DISCOVERY_CACHE: dict[tuple, "McpDiscovery"] = {}
_MCP_LOG_DIR = Path(script_dir) / "temp" / "mcp_logs"
_MCP_TOOLS_CACHE_PATH = Path(script_dir) / "temp" / "mcp_tools_cache.json"
# Incomplete discovery (a server failed/timed out) is cached only briefly so a
# transient remote-server timeout cannot permanently hide its tools. Complete
# results stay cached as long as the config signature matches (no TTL).
_MCP_TOOLS_CACHE_INCOMPLETE_TTL = 60.0
_MAX_MCP_DESCRIPTION_LENGTH = 2048


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
    stderr_log: Any = None
    connect_future: Any = None


_MANAGER_LOCK = threading.Lock()
_MANAGER: Optional["McpManager"] = None
_CALL_CONTEXT = threading.local()


@contextmanager
def mcp_cancellation_scope(stop_signal):
    previous = getattr(_CALL_CONTEXT, "stop_signal", None)
    _CALL_CONTEXT.stop_signal = stop_signal
    try:
        yield
    finally:
        if previous is None:
            try:
                del _CALL_CONTEXT.stop_signal
            except AttributeError:
                pass
        else:
            _CALL_CONTEXT.stop_signal = previous


def _current_stop_signal():
    return getattr(_CALL_CONTEXT, "stop_signal", None)


def _stop_requested(stop_signal) -> bool:
    if stop_signal is None:
        return False
    is_set = getattr(stop_signal, "is_set", None)
    return bool(is_set()) if callable(is_set) else bool(stop_signal)


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
        self._tracked_tasks: set[asyncio.Task] = set()
        self._local_connect_semaphore: Optional[asyncio.Semaphore] = None
        self._closing = False
        self.loop = asyncio.new_event_loop()
        self.loop_thread = threading.Thread(
            target=self.loop.run_forever,
            daemon=True,
            name="ga-mcp-loop",
        )
        self.loop_thread.start()
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
        self.ensure_all_connected(timeout=timeout, retry_failed=True)
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
        if self._closing:
            return
        self._closing = True
        with self.lock:
            states = list(self.states.values())
        if self.loop.is_running():
            shutdown = asyncio.run_coroutine_threadsafe(self._shutdown_async(states), self.loop)
            try:
                shutdown.result(timeout=15)
            except Exception:
                pass
            self.loop.call_soon_threadsafe(self.loop.stop)
        if self.loop_thread.is_alive():
            self.loop_thread.join(timeout=5)
        if not self.loop_thread.is_alive() and not self.loop.is_closed():
            self.loop.close()

    async def _shutdown_async(self, states: list[McpServerState]) -> None:
        current = asyncio.current_task()
        tracked = [task for task in self._tracked_tasks if task is not current and not task.done()]
        for task in tracked:
            task.cancel()
        if tracked:
            await asyncio.wait(tracked, timeout=8)

        for state in states:
            try:
                await self._close_state_async(state)
            except Exception:
                pass

        remaining = [
            task
            for task in asyncio.all_tasks()
            if task is not current and not task.done()
        ]
        for task in remaining:
            task.cancel()
        if remaining:
            await asyncio.wait(remaining, timeout=3)
        await asyncio.sleep(0.1)

    def discover(self, include_unavailable: bool = False, timeout: Optional[float] = None) -> McpDiscovery:
        self.ensure_all_connected(timeout=timeout, retry_failed=False)
        discovery = McpDiscovery()
        with self.lock:
            for state in self.states.values():
                if state.status == "disabled":
                    if include_unavailable:
                        discovery.errors[state.name] = "disabled"
                    continue
                if state.error:
                    discovery.errors[state.name] = state.error
                    if not include_unavailable:
                        continue
                discovery.tools.extend(dict(tool) for tool in state.tools)
                discovery.tool_refs.update(state.tool_refs)
        return discovery

    def ensure_all_connected(self, timeout: Optional[float] = None, retry_failed: bool = True) -> None:
        wait_timeout = _default_timeout(timeout)
        stop_signal = _current_stop_signal()
        if _stop_requested(stop_signal):
            return
        with self.lock:
            states = []
            for state in self.states.values():
                if state.status == "disabled" or state.client is not None:
                    continue
                if state.status == "failed" and not retry_failed:
                    continue
                states.append(state)
        futures = []
        for state in states:
            future = self._connect_future(state)
            if future is not None and future not in futures:
                futures.append(future)
        if futures:
            deadline = time.monotonic() + max(wait_timeout + 1.0, wait_timeout * 2)
            for future in futures:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                try:
                    self._wait_future(
                        future,
                        timeout=remaining,
                        stop_signal=stop_signal,
                        cancel_on_stop=False,
                        cancel_on_timeout=False,
                    )
                except asyncio.CancelledError:
                    return
                except TimeoutError:
                    break

    def _connect_future(self, state: McpServerState):
        with self.lock:
            if state.status == "connected" and state.client is not None:
                return None
            future = state.connect_future
            if future is None or future.done():
                state.status = "pending"
                state.error = ""
                startup_timeout = _default_timeout(state.config.get("startup_timeout_sec"))
                future = self._submit(self._connect_state(state, startup_timeout))
                state.connect_future = future
                future.add_done_callback(lambda done, target=state: self._clear_connect_future(target, done))
            return future

    async def _connect_state(self, state: McpServerState, timeout: float) -> None:
        if not _is_local_mcp_server(state.config):
            await self._connect_and_fetch_tools(state, timeout)
            return
        if self._local_connect_semaphore is None:
            self._local_connect_semaphore = asyncio.Semaphore(1)
        async with self._local_connect_semaphore:
            await self._connect_and_fetch_tools(state, timeout)

    def ensure_connected(self, server_name: str, timeout: Optional[float] = None, stop_signal=None) -> McpServerState:
        wait_timeout = _default_timeout(timeout)
        if _stop_requested(stop_signal):
            raise asyncio.CancelledError("MCP connection wait aborted by user")
        with self.lock:
            state = self.states[server_name]
            if state.status == "disabled":
                return state
            if state.status == "connected" and state.client is not None:
                return state
        future = self._connect_future(state)
        if future is not None:
            self._wait_future(
                future,
                timeout=wait_timeout + 1.0,
                stop_signal=stop_signal,
                cancel_on_stop=False,
                cancel_on_timeout=False,
            )
        return state

    def _clear_connect_future(self, state: McpServerState, future) -> None:
        with self.lock:
            if state.connect_future is future:
                state.connect_future = None

    def call_tool(
        self,
        full_name: str,
        arguments: Optional[dict[str, Any]] = None,
        timeout: Optional[float] = None,
    ) -> dict[str, Any]:
        call_timeout = _default_timeout(timeout, env_name="GA_MCP_CALL_TIMEOUT", fallback=60)
        stop_signal = _current_stop_signal()
        server_name = self._server_name_for_tool(full_name)
        if server_name is None:
            known = ", ".join(sorted(self.states)[:30])
            return {
                "status": "error",
                "msg": f"Unknown MCP server for tool: {full_name}" + (f". Known servers: {known}" if known else ""),
                "discovery_errors": {},
            }
        with self.lock:
            state = self.states[server_name]
            if state.status == "disabled":
                return {
                    "status": "error",
                    "msg": f"MCP server is disabled: {server_name}",
                    "discovery_errors": {server_name: "disabled"},
                }
        try:
            self.ensure_connected(server_name, timeout=call_timeout, stop_signal=stop_signal)
        except TimeoutError as e:
            return {"status": "error", "msg": _redact_sensitive(f"TimeoutError: {e}")}
        except asyncio.CancelledError:
            return {"status": "error", "msg": "MCP call aborted by user"}
        with self.lock:
            state = self.states[server_name]
            tool_ref = state.tool_refs.get(full_name)
            server_error = state.error
            known_refs = sorted(state.tool_refs)[:30]
        if tool_ref is None:
            known = ", ".join(known_refs)
            if server_error:
                msg = f"MCP server {server_name} unavailable: {server_error}"
            else:
                msg = f"Unknown MCP tool: {full_name}" + (f". Known for {server_name}: {known}" if known else "")
            return {
                "status": "error",
                "msg": msg,
                "discovery_errors": {server_name: server_error} if server_error else {},
            }
        clean_args = {k: v for k, v in (arguments or {}).items() if not str(k).startswith("_")}
        try:
            return self._run(
                self._call_tool_async(state, tool_ref.tool_name, clean_args, timeout=call_timeout),
                timeout=call_timeout + 1.0,
                stop_signal=stop_signal,
            )
        except TimeoutError as e:
            self._mark_state_interrupted(state)
            return {"status": "error", "msg": _redact_sensitive(f"TimeoutError: {e}")}
        except asyncio.CancelledError:
            self._mark_state_interrupted(state)
            return {"status": "error", "msg": "MCP call aborted by user"}

    def _server_name_for_tool(self, full_name: str) -> Optional[str]:
        text = str(full_name or "")
        if not text.startswith(MCP_TOOL_PREFIX):
            return None
        with self.lock:
            matches = [
                name
                for name in self.states
                if text.startswith(f"{MCP_TOOL_PREFIX}{normalize_mcp_name(name)}__")
            ]
        if not matches:
            return None
        return max(matches, key=lambda name: len(f"{MCP_TOOL_PREFIX}{normalize_mcp_name(name)}__"))

    def reconnect(self, server_name: str, timeout: Optional[float] = None) -> dict[str, Any]:
        with self.lock:
            if server_name not in self.states:
                raise KeyError(f"Unknown MCP server: {server_name}")
            state = self.states[server_name]
            self._close_state(state)
            if state.status != "disabled":
                state.status = "pending"
            state.error = ""
            state.tools = []
            state.tool_refs = {}
        self.ensure_connected(server_name, timeout=timeout)
        with self.lock:
            return {"server": self._server_summary(self.states[server_name])}

    def enable(self, server_name: str, timeout: Optional[float] = None) -> dict[str, Any]:
        set_mcp_server_enabled(server_name, True, self.config_path)
        return self.reconnect(server_name, timeout=timeout)

    def disable(self, server_name: str) -> dict[str, Any]:
        set_mcp_server_enabled(server_name, False, self.config_path)
        with self.lock:
            if server_name not in self.states:
                raise KeyError(f"Unknown MCP server: {server_name}")
            state = self.states[server_name]
            self._close_state(state)
            state.status = "disabled"
            state.error = ""
            state.tools = []
            state.tool_refs = {}
            return {"server": self._server_summary(state)}

    def _run(self, coro, timeout: Optional[float] = None, stop_signal=None, poll_interval: float = 0.1):
        """Run a coroutine on the manager loop without permanently blocking the caller.

        The old future.result() path could hang forever if an MCP tool timed out
        but the underlying transport refused to cancel. Poll with a short interval
        so hard timeouts and /stop can free the agent thread.
        """
        future = self._submit(coro)
        return self._wait_future(
            future,
            timeout=timeout,
            stop_signal=stop_signal,
            poll_interval=poll_interval,
        )

    def _submit(self, coro):
        if self._closing or not self.loop.is_running():
            coro.close()
            raise RuntimeError("MCP manager is closed")
        return asyncio.run_coroutine_threadsafe(self._track_task(coro), self.loop)

    def _wait_future(
        self,
        future,
        timeout: Optional[float] = None,
        stop_signal=None,
        poll_interval: float = 0.1,
        cancel_on_stop: bool = True,
        cancel_on_timeout: bool = True,
    ):
        deadline = None if timeout is None else (time.monotonic() + float(timeout))
        interval = max(0.05, float(poll_interval))
        while True:
            if _stop_requested(stop_signal):
                if cancel_on_stop:
                    future.cancel()
                raise asyncio.CancelledError("MCP call aborted by stop signal")
            remaining = None
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    if cancel_on_timeout:
                        future.cancel()
                    raise TimeoutError(f"MCP operation timed out after {timeout}s")
            wait_for = interval if remaining is None else min(interval, remaining)
            try:
                return future.result(timeout=wait_for)
            except concurrent.futures.TimeoutError:
                continue
            except concurrent.futures.CancelledError as e:
                raise asyncio.CancelledError("MCP operation cancelled") from e

    async def _track_task(self, coro):
        task = asyncio.current_task()
        if task is not None:
            self._tracked_tasks.add(task)
        try:
            return await coro
        finally:
            if task is not None:
                self._tracked_tasks.discard(task)

    async def _connect_and_fetch_tools(self, state: McpServerState, timeout: float) -> None:
        try:
            await self._close_state_async(state)
            await self._open_state_client(state, timeout)
            tools = await asyncio.wait_for(state.client.list_tools(), timeout=timeout)
            schemas: list[dict[str, Any]] = []
            refs: dict[str, McpToolRef] = {}
            seen: set[str] = set()
            for tool in tools:
                original_name = str(getattr(tool, "name", ""))
                if not original_name:
                    continue
                full_name = build_mcp_tool_name(state.name, original_name)
                if full_name in seen:
                    state.error = f"Duplicate normalized MCP tool name skipped: {full_name}"
                    continue
                seen.add(full_name)
                schema = _tool_to_function_schema(state.name, tool, full_name)
                schemas.append(schema)
                refs[full_name] = McpToolRef(
                    full_name=full_name,
                    server_name=state.name,
                    tool_name=original_name,
                    server_config=dict(state.config),
                    schema=schema,
                )
            with self.lock:
                state.tools = schemas
                state.tool_refs = refs
                state.status = "connected"
                state.error = ""
        except Exception as e:
            await self._close_state_async(state)
            with self.lock:
                state.status = "failed"
                state.error = _redact_sensitive(f"{type(e).__name__}: {e}")
                state.tools = []
                state.tool_refs = {}

    async def _open_state_client(self, state: McpServerState, timeout: float) -> None:
        from fastmcp import Client
        from fastmcp.client.transports import MCPConfigTransport, StdioTransport

        transport = MCPConfigTransport(_single_server_config(state.name, state.config))
        stderr_log = None
        if isinstance(getattr(transport, "transport", None), StdioTransport):
            transport.transport.keep_alive = True
            _MCP_LOG_DIR.mkdir(parents=True, exist_ok=True)
            log_name = f"{normalize_mcp_name(state.name)}.stderr.log"
            stderr_log = (_MCP_LOG_DIR / log_name).open("a", encoding="utf-8", errors="replace")
            transport.transport.log_file = stderr_log
        try:
            with _stdio_errlog_patch(transport, stderr_log):
                entered = _make_fastmcp_client(Client, transport, state.name, timeout)
                client = await entered.__aenter__()
        except BaseException:
            if stderr_log is not None:
                stderr_log.close()
            raise
        with self.lock:
            state.entered = entered
            state.client = client
            state.stderr_log = stderr_log

    async def _call_tool_async(
        self,
        state: McpServerState,
        tool_name: str,
        arguments: dict[str, Any],
        timeout: float,
    ) -> dict[str, Any]:
        try:
            result = await asyncio.wait_for(
                state.client.call_tool(tool_name, arguments, timeout=timeout, raise_on_error=False),
                timeout=timeout,
            )
            return _serialize_call_result(result)
        except TimeoutError:
            raise
        except Exception as e:
            with self.lock:
                state.status = "failed"
                state.error = _redact_sensitive(f"{type(e).__name__}: {e}")
            return {"status": "error", "msg": state.error}

    def _mark_state_interrupted(self, state: McpServerState) -> None:
        self._close_state(state)
        with self.lock:
            state.status = "pending"
            state.error = ""

    def _close_state(self, state: McpServerState) -> None:
        if state.entered is not None:
            try:
                self._run(self._close_state_async(state), timeout=8.0)
            except Exception:
                # Fall through and drop local refs even if the transport is stuck.
                pass
        if state.stderr_log is not None:
            try:
                state.stderr_log.close()
            except Exception:
                pass
        state.client = None
        state.entered = None
        state.stderr_log = None

    async def _close_state_async(self, state: McpServerState) -> None:
        entered = state.entered
        client = state.client
        stderr_log = state.stderr_log
        state.client = None
        state.entered = None
        state.stderr_log = None
        try:
            close = getattr(client or entered, "close", None)
            if close is not None:
                await close()
            elif entered is not None:
                await entered.__aexit__(None, None, None)
        finally:
            if stderr_log is not None:
                stderr_log.close()

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
        if _MANAGER is not None and _MANAGER.config_path != path:
            _MANAGER.close()
            _MANAGER = None
        if _MANAGER is None:
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


def reconnect_mcp_server(
    server_name: str,
    config_path: Optional[os.PathLike | str] = None,
    timeout: Optional[float] = None,
) -> dict[str, Any]:
    return get_mcp_manager(config_path).reconnect(server_name, timeout=timeout)


def enable_mcp_server(
    server_name: str,
    config_path: Optional[os.PathLike | str] = None,
    timeout: Optional[float] = None,
) -> dict[str, Any]:
    return get_mcp_manager(config_path).enable(server_name, timeout=timeout)


def disable_mcp_server(
    server_name: str,
    config_path: Optional[os.PathLike | str] = None,
) -> dict[str, Any]:
    return get_mcp_manager(config_path).disable(server_name)


def discover_mcp_tools(
    config_path: Optional[os.PathLike | str] = None,
    include_unavailable: bool = False,
    timeout: Optional[float] = None,
) -> list[dict[str, Any]]:
    discovery = discover_mcp(config_path=config_path, include_unavailable=include_unavailable, timeout=timeout)
    return [dict(tool) for tool in discovery.tools]


def discover_mcp_tools_cached(
    config_path: Optional[os.PathLike | str] = None,
    include_unavailable: bool = False,
    timeout: Optional[float] = None,
    cache_path: Optional[os.PathLike | str] = None,
) -> list[dict[str, Any]]:
    cfg = load_mcp_config_with_disabled(config_path)
    signature = _cache_signature(cfg, include_unavailable)
    cache_file = Path(cache_path) if cache_path is not None else _MCP_TOOLS_CACHE_PATH
    cached = _read_mcp_tools_cache(cache_file, signature)
    if cached is not None:
        return cached
    discovery = discover_mcp(
        config_path=config_path,
        include_unavailable=include_unavailable,
        timeout=timeout,
    )
    tools = [dict(tool) for tool in discovery.tools]
    if _stop_requested(_current_stop_signal()):
        return tools
    # A server that failed/timed out (errors non-empty) means the tool set is
    # partial; cache it only briefly so a transient remote timeout self-heals.
    complete = not discovery.errors
    _write_mcp_tools_cache(cache_file, signature, tools, complete=complete)
    return tools


def discover_mcp(
    config_path: Optional[os.PathLike | str] = None,
    include_unavailable: bool = False,
    timeout: Optional[float] = None,
) -> McpDiscovery:
    return get_mcp_manager(config_path).discover(
        include_unavailable=include_unavailable,
        timeout=timeout,
    )


def call_mcp_tool(
    full_name: str,
    arguments: Optional[dict[str, Any]] = None,
    config_path: Optional[os.PathLike | str] = None,
    timeout: Optional[float] = None,
) -> dict[str, Any]:
    return get_mcp_manager(config_path).call_tool(
        full_name,
        arguments=arguments,
        timeout=timeout,
    )


def _default_timeout(value: Optional[float], env_name: str = "GA_MCP_DISCOVERY_TIMEOUT", fallback: float = 8) -> float:
    if value is not None:
        return float(value)
    try:
        return float(os.environ.get(env_name, fallback))
    except (TypeError, ValueError):
        return fallback


def _make_fastmcp_client(Client, transport, server_name: str, timeout: float):
    try:
        return Client(transport, name=f"ga-mcp-{server_name}", timeout=timeout, init_timeout=timeout)
    except TypeError as e:
        if "unexpected keyword argument 'name'" not in str(e):
            raise
        return Client(transport, timeout=timeout, init_timeout=timeout)


def _is_local_mcp_server(server_config: dict[str, Any]) -> bool:
    cfg_type = str(server_config.get("transport") or server_config.get("type") or "").lower()
    return bool(server_config.get("command") or cfg_type == "stdio")


def _cache_signature(cfg: McpConfig, include_unavailable: bool) -> dict[str, Any]:
    file_sig = None
    if cfg.path:
        try:
            stat = cfg.path.stat()
            file_sig = {
                "path": str(cfg.path.resolve(strict=False)),
                "mtime_ns": stat.st_mtime_ns,
                "size": stat.st_size,
            }
        except OSError:
            file_sig = {"path": str(cfg.path), "mtime_ns": None, "size": None}
    return {
        "file": file_sig,
        "servers": sorted(cfg.servers),
        "include_unavailable": bool(include_unavailable),
    }


def _read_mcp_tools_cache(cache_path: Path, signature: dict[str, Any]) -> Optional[list[dict[str, Any]]]:
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if data.get("signature") != signature:
        return None
    tools = data.get("tools")
    if not isinstance(tools, list):
        return None
    # Incomplete results (a server failed at discovery) expire after a short TTL
    # so a transient remote-server timeout does not permanently hide its tools.
    if not data.get("complete", True):
        cached_at = data.get("cached_at")
        if not isinstance(cached_at, (int, float)):
            return None
        if time.time() - cached_at > _MCP_TOOLS_CACHE_INCOMPLETE_TTL:
            return None
    return [dict(tool) for tool in tools if isinstance(tool, dict)]


def _write_mcp_tools_cache(
    cache_path: Path,
    signature: dict[str, Any],
    tools: list[dict[str, Any]],
    complete: bool = True,
) -> None:
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(
            json.dumps(
                {"signature": signature, "tools": tools, "cached_at": time.time(), "complete": bool(complete)},
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
    except Exception:
        pass


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
        with _stdio_errlog_patch(transport, stderr_log):
            async with _make_fastmcp_client(Client, transport, server_name, timeout) as client:
                yield client
    finally:
        if stderr_log is not None:
            stderr_log.close()


@contextmanager
def _stdio_errlog_patch(transport: Any, stderr_log: Any):
    if stderr_log is None:
        yield
        return
    stdio_transport = getattr(transport, "transport", None)
    try:
        from fastmcp.client.transports import StdioTransport
    except Exception:
        StdioTransport = None
    if StdioTransport is None or not isinstance(stdio_transport, StdioTransport):
        yield
        return

    original_connect_session = stdio_transport.connect_session

    @asynccontextmanager
    async def connect_session_with_errlog(**session_kwargs):
        from mcp.client.stdio import StdioServerParameters, stdio_client
        from mcp import ClientSession

        server_params = StdioServerParameters(
            command=stdio_transport.command,
            args=stdio_transport.args,
            env=stdio_transport.env,
            cwd=stdio_transport.cwd,
        )
        async with stdio_client(server_params, errlog=stderr_log) as transport_pair:
            read_stream, write_stream = transport_pair
            async with ClientSession(read_stream, write_stream, **session_kwargs) as session:
                yield session

    stdio_transport.connect_session = connect_session_with_errlog
    try:
        yield
    finally:
        stdio_transport.connect_session = original_connect_session


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
    if len(description) > _MAX_MCP_DESCRIPTION_LENGTH:
        description = description[:_MAX_MCP_DESCRIPTION_LENGTH] + "... [truncated]"
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
    return redact_sensitive_text(text)


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
