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
# A partial result is handed to callers as-is, but the missing servers are
# retried in the background on this cadence. This is the old 60s self-heal
# TTL, moved off the critical path: a transient remote timeout no longer
# blocks a user turn to rediscover tools.
_MCP_TOOLS_CACHE_PARTIAL_RECHECK_TTL = _MCP_TOOLS_CACHE_INCOMPLETE_TTL
# Even a complete result is re-checked now and then so a server that starts
# publishing new tools is eventually reflected. Not a correctness TTL: the old
# value keeps being served while the refresh runs in the background.
_MCP_TOOLS_CACHE_REVERIFY_TTL = 600.0
# How long the first MCP-aware turn waits for tools when no cache exists yet.
# Local (stdio) servers settle in milliseconds; remote ones keep connecting in
# the background and land in the schema when they are ready.
_MCP_DISCOVERY_BUDGET_DEFAULT = 2.0
# Per-server connect/initialize budget. Codex uses DEFAULT_STARTUP_TIMEOUT = 30s
# (codex-mcp/src/rmcp_client.rs). The old 8s sat in the middle of the remote
# TLS+initialize handshake distribution, so a merely slow server read as a hard
# failure and stayed failed for the rest of the process.
_MCP_DISCOVERY_TIMEOUT_DEFAULT = 30.0
# Remote (HTTP/SSE) initialize retries, mirroring Codex's
# STREAMABLE_HTTP_RETRY_DELAYS_MS = [250, 1000]
# (rmcp-client/src/streamable_http_retry.rs). stdio is not retried: a local
# process that died will die again.
_MCP_REMOTE_CONNECT_ATTEMPTS = 3
_MCP_REMOTE_CONNECT_RETRY_DELAYS = (0.25, 1.0)
# Local stdio servers used to connect one at a time behind Semaphore(1), which
# serialized three npx cold starts into a 12s+ discovery. Codex starts every
# server concurrently; keep a bounded pool so npx cache contention stays finite.
_MCP_LOCAL_CONNECT_CONCURRENCY_DEFAULT = 4
# stderr from stdio servers is a debug log; bound it so it cannot grow forever.
_MCP_STDERR_LOG_MAX_BYTES_DEFAULT = 256 * 1024
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

# Background tool discovery shared by every caller in the process. A user turn
# reads whatever is already cached or connected and never waits on a slow MCP
# server; this state tracks the single refresh worker that fills the gap.
_MCP_DISCOVERY_LOCK = threading.Lock()
_MCP_DISCOVERY_CALLBACKS: list = []
_MCP_DISCOVERY_THREAD: Optional[threading.Thread] = None
_MCP_DISCOVERY_STATE: dict[str, Any] = {
    "running": False,
    "last_started_at": None,
    "last_finished_at": None,
    "last_error": None,
    "last_errors": {},
    "last_skips": [],
    "tool_count": 0,
    "complete": None,
}


def _record_discovery_skip(reason: str, prior_tools: int) -> None:
    """Note a discovery pass that kept old tools instead of trusting an empty result."""
    with _MCP_DISCOVERY_LOCK:
        skips = list(_MCP_DISCOVERY_STATE.get("last_skips") or [])
        skips.append({"reason": reason, "prior_tools": int(prior_tools), "at": time.time()})
        _MCP_DISCOVERY_STATE["last_skips"] = skips[-10:]


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


def mcp_discovery_state() -> dict[str, Any]:
    """Snapshot of the background discovery worker, for diagnostics and tests."""
    return mcp_discovery_warmup_state()


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
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        """Return currently known MCP state without starting or waiting on connections."""
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

    def discover(
        self,
        include_unavailable: bool = False,
        timeout: Optional[float] = None,
        retry_failed: bool = False,
    ) -> McpDiscovery:
        self.ensure_all_connected(timeout=timeout, retry_failed=retry_failed)
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
                state.status = "connecting"
                state.error = ""
                startup_timeout = _default_timeout(state.config.get("startup_timeout_sec"))
                future = self._submit(self._connect_state(state, startup_timeout))
                state.connect_future = future
                future.add_done_callback(lambda done, target=state: self._clear_connect_future(target, done))
            return future

    async def _connect_state(self, state: McpServerState, timeout: float) -> None:
        if _is_local_mcp_server(state.config):
            semaphore = self._local_connect_semaphore
            if semaphore is None:
                semaphore = asyncio.Semaphore(_local_connect_concurrency())
                self._local_connect_semaphore = semaphore
            with self.lock:
                state.status = "connecting"
            async with semaphore:
                await self._connect_and_fetch_tools(state, timeout)
            return
        # Remote transports get bounded initialize retries, mirroring Codex: a
        # transient TLS/handshake blip must not read as a permanent failure.
        budget = max(0.05, float(timeout))
        deadline = time.monotonic() + budget
        for attempt in range(_MCP_REMOTE_CONNECT_ATTEMPTS):
            remaining = max(0.05, deadline - time.monotonic())
            with self.lock:
                state.status = "connecting"
            await self._connect_and_fetch_tools(state, min(budget, remaining))
            if state.status == "connected":
                return
            if attempt >= _MCP_REMOTE_CONNECT_ATTEMPTS - 1:
                return
            if not _is_retryable_connect_error(state.error):
                return
            delay = _MCP_REMOTE_CONNECT_RETRY_DELAYS[
                min(attempt, len(_MCP_REMOTE_CONNECT_RETRY_DELAYS) - 1)
            ]
            if deadline - time.monotonic() <= delay:
                return
            with self.lock:
                state.status = "connecting"
            await asyncio.sleep(delay)

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
            stderr_log = _open_stderr_log(state.name)
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


def mcp_status_snapshot(config_path: Optional[os.PathLike | str] = None) -> dict[str, Any]:
    """Return an immediate, non-blocking snapshot suitable for live UI progress."""
    payload = get_mcp_manager(config_path).snapshot()
    warmup = mcp_discovery_warmup_state()
    loading = bool(warmup.get("running")) or any(
        server.get("status") in {"pending", "connecting"}
        for server in payload["servers"]
    )
    payload.update({
        "loading": loading,
        "discovery_running": bool(warmup.get("running")),
        "discovery_complete": warmup.get("complete"),
    })
    return payload


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


def available_mcp_tools(
    config_path: Optional[os.PathLike | str] = None,
    include_unavailable: bool = False,
) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """Non-blocking snapshot of whatever the manager has connected so far."""
    manager = get_mcp_manager(config_path)
    tools: list[dict[str, Any]] = []
    errors: dict[str, str] = {}
    with manager.lock:
        for state in manager.states.values():
            if state.status == "disabled":
                if include_unavailable:
                    errors[state.name] = "disabled"
                continue
            if state.error and not include_unavailable:
                errors[state.name] = state.error
                continue
            tools.extend(dict(tool) for tool in state.tools)
    return tools, errors


def _mcp_discovery_budget(value: Optional[float] = None) -> float:
    if value is not None:
        return max(0.0, float(value))
    try:
        return max(0.0, float(os.environ.get("GA_MCP_DISCOVERY_BUDGET", _MCP_DISCOVERY_BUDGET_DEFAULT)))
    except (TypeError, ValueError):
        return _MCP_DISCOVERY_BUDGET_DEFAULT


def _discovery_cache_file(cache_path: Optional[os.PathLike | str]) -> Path:
    return Path(cache_path) if cache_path is not None else _MCP_TOOLS_CACHE_PATH


def on_mcp_discovery_complete(callback) -> None:
    """Register a listener notified with (tools, complete) after a background refresh."""
    with _MCP_DISCOVERY_LOCK:
        if callback not in _MCP_DISCOVERY_CALLBACKS:
            _MCP_DISCOVERY_CALLBACKS.append(callback)


def remove_mcp_discovery_listener(callback) -> None:
    with _MCP_DISCOVERY_LOCK:
        try:
            _MCP_DISCOVERY_CALLBACKS.remove(callback)
        except ValueError:
            pass


def _notify_mcp_discovery_complete(tools: list[dict[str, Any]], complete: bool) -> None:
    with _MCP_DISCOVERY_LOCK:
        callbacks = list(_MCP_DISCOVERY_CALLBACKS)
    for callback in callbacks:
        try:
            callback(tools, complete)
        except Exception:
            continue


def _tool_server_name(tool: dict[str, Any]) -> str:
    name = ((tool.get("function") or {}).get("name") or "")
    parts = name.split("__")
    return parts[1] if len(parts) >= 3 else ""


def _merge_partial_discovery(
    tools: list[dict[str, Any]],
    previous: Optional[dict[str, Any]],
    failed_servers: set[str],
) -> list[dict[str, Any]]:
    """Keep previously known tools for servers that failed this pass.

    A discovery pass is not authoritative about a server it never reached. Losing
    the handshake for one server must not delete its tools from the cache, which
    is how the 2026-10-01 run ended up serving zero MCP tools for a whole task.
    """
    if not previous or not previous.get("tools"):
        return tools
    kept: list[dict[str, Any]] = []
    for tool in previous["tools"]:
        if not isinstance(tool, dict):
            continue
        if _tool_server_name(tool) in failed_servers and tool not in tools:
            kept.append(dict(tool))
    if not kept:
        return tools
    seen = {((tool.get("function") or {}).get("name") or "") for tool in tools}
    for tool in kept:
        name = (tool.get("function") or {}).get("name") or ""
        if name and name not in seen:
            tools.append(tool)
            seen.add(name)
    return tools


def _background_discovery_worker(
    config_path: Optional[os.PathLike | str],
    include_unavailable: bool,
    timeout: Optional[float],
    cache_path: Optional[os.PathLike | str],
    retry_failed: bool = True,
) -> None:
    global _MCP_DISCOVERY_THREAD
    tools: list[dict[str, Any]] = []
    complete = False
    error: Optional[str] = None
    errors: dict[str, str] = {}
    try:
        discovery = discover_mcp(
            config_path=config_path,
            include_unavailable=include_unavailable,
            timeout=timeout,
            retry_failed=retry_failed,
        )
        tools = [dict(tool) for tool in discovery.tools]
        errors = dict(discovery.errors)
        complete = not discovery.errors
        cfg = load_mcp_config_with_disabled(config_path)
        signature = _cache_signature(cfg, include_unavailable)
        cache_file = _discovery_cache_file(cache_path)
        previous = _load_mcp_tools_cache_entry(cache_file, signature, allow_stale=True)
        if errors:
            prior_tools = len((previous or {}).get("tools") or [])
            if not tools and prior_tools:
                # Reaching nothing at all is a failed pass, not proof the known
                # tools are gone; keep serving them and retry in the background.
                tools = [dict(tool) for tool in previous["tools"] if isinstance(tool, dict)]
                complete = False
                _record_discovery_skip("no_server_reached", prior_tools)
            else:
                tools = _merge_partial_discovery(tools, previous, set(errors))
        _write_mcp_tools_cache(cache_file, signature, tools, complete=complete)
    except Exception as e:
        error = _redact_sensitive(f"{type(e).__name__}: {e}")
    finally:
        with _MCP_DISCOVERY_LOCK:
            _MCP_DISCOVERY_STATE.update(
                {
                    "last_finished_at": time.time(),
                    "last_error": error,
                    "last_errors": errors,
                    "tool_count": len(tools),
                    "complete": complete,
                }
            )
            _MCP_DISCOVERY_THREAD = None
    if error is None:
        _notify_mcp_discovery_complete(tools, complete)


def start_background_discovery(
    config_path: Optional[os.PathLike | str] = None,
    include_unavailable: bool = False,
    timeout: Optional[float] = None,
    cache_path: Optional[os.PathLike | str] = None,
    retry_failed: bool = True,
) -> Optional[threading.Thread]:
    """Discover MCP tools off the caller thread; a no-op while one is running.

    The background pass is the self-heal channel, so it retries servers that
    previously failed; the synchronous discovery helpers keep ``retry_failed``
    off so they never hammer a server that is known to be down.
    """
    global _MCP_DISCOVERY_THREAD
    with _MCP_DISCOVERY_LOCK:
        if _MCP_DISCOVERY_THREAD is not None and _MCP_DISCOVERY_THREAD.is_alive():
            return _MCP_DISCOVERY_THREAD
        _MCP_DISCOVERY_STATE.update({"last_started_at": time.time()})
        thread = threading.Thread(
            target=_background_discovery_worker,
            args=(config_path, include_unavailable, timeout, cache_path, retry_failed),
            name="ga-mcp-discovery",
            daemon=True,
        )
        _MCP_DISCOVERY_THREAD = thread
        thread.start()
        return thread


def wait_for_background_discovery(
    budget: Optional[float] = None,
    thread: Optional[threading.Thread] = None,
) -> bool:
    """Wait up to ``budget`` seconds for the in-flight discovery. True when it finished."""
    seconds = _mcp_discovery_budget(budget)
    with _MCP_DISCOVERY_LOCK:
        target = thread if thread is not None else _MCP_DISCOVERY_THREAD
    if target is None:
        return True
    if seconds <= 0:
        return not target.is_alive()
    target.join(timeout=seconds)
    return not target.is_alive()


def mcp_discovery_warmup_state() -> dict[str, Any]:
    with _MCP_DISCOVERY_LOCK:
        state = dict(_MCP_DISCOVERY_STATE)
        thread = _MCP_DISCOVERY_THREAD
    state["running"] = bool(thread is not None and thread.is_alive())
    return state


def discover_mcp(
    config_path: Optional[os.PathLike | str] = None,
    include_unavailable: bool = False,
    timeout: Optional[float] = None,
    retry_failed: bool = False,
) -> McpDiscovery:
    return get_mcp_manager(config_path).discover(
        include_unavailable=include_unavailable,
        timeout=timeout,
        retry_failed=retry_failed,
    )


def _maybe_schedule_discovery_refresh(
    cached: dict[str, Any],
    config_path: Optional[os.PathLike | str],
    include_unavailable: bool,
    timeout: Optional[float],
    cache_path: Optional[os.PathLike | str],
) -> bool:
    cached_at = cached.get("cached_at")
    age = None if not isinstance(cached_at, (int, float)) else time.time() - cached_at
    if not cached.get("complete", True):
        # A server timed out at discovery: retry it soon, but never block a turn.
        stale = age is None or age > _MCP_TOOLS_CACHE_PARTIAL_RECHECK_TTL
    else:
        stale = age is None or age > _MCP_TOOLS_CACHE_REVERIFY_TTL
    if not stale:
        return False
    start_background_discovery(
        config_path=config_path,
        include_unavailable=include_unavailable,
        timeout=timeout,
        cache_path=cache_path,
    )
    return True


def _merge_live_mcp_tools(
    cached_tools: list[dict[str, Any]],
    config_path: Optional[os.PathLike | str],
    include_unavailable: bool,
) -> list[dict[str, Any]]:
    """Union cached tools with everything already connected in this process.

    The background worker writes its cache only once a whole pass finishes, so a
    server that connected mid-pass is otherwise invisible to a turn. Observed
    2026-10-01: a task ran end to end with zero MCP tools while six servers were
    reachable seconds later.
    """
    tools = [dict(tool) for tool in cached_tools or []]
    try:
        live, _errors = available_mcp_tools(config_path=config_path, include_unavailable=include_unavailable)
    except Exception:
        return tools
    if not live:
        return tools
    seen = {((tool.get("function") or {}).get("name") or "") for tool in tools}
    for tool in live:
        name = (tool.get("function") or {}).get("name") or ""
        if name and name not in seen:
            tools.append(dict(tool))
            seen.add(name)
    return tools


def discover_mcp_tools_cached_fast(
    config_path: Optional[os.PathLike | str] = None,
    include_unavailable: bool = False,
    timeout: Optional[float] = None,
    cache_path: Optional[os.PathLike | str] = None,
    budget: Optional[float] = None,
) -> list[dict[str, Any]]:
    """Tool discovery that never blocks a user turn for long.

    A cache hit (including a partial or aged one) returns immediately and the
    refresh happens in the background. Without a cache this waits at most
    ``budget`` seconds (``GA_MCP_DISCOVERY_BUDGET``, default 2s) before handing
    back whatever is already connected, so a slow remote server only costs the
    first turn a bounded delay instead of stalling every turn.
    """
    cfg = load_mcp_config_with_disabled(config_path)
    signature = _cache_signature(cfg, include_unavailable)
    cache_file = _discovery_cache_file(cache_path)
    cached = _load_mcp_tools_cache_entry(cache_file, signature, allow_stale=True)
    if cached is not None:
        _maybe_schedule_discovery_refresh(cached, config_path, include_unavailable, timeout, cache_path)
        return _merge_live_mcp_tools(cached["tools"], config_path, include_unavailable)
    thread = start_background_discovery(
        config_path=config_path,
        include_unavailable=include_unavailable,
        timeout=timeout,
        cache_path=cache_path,
    )
    wait_for_background_discovery(budget, thread=thread)
    cached = _load_mcp_tools_cache_entry(cache_file, signature, allow_stale=True)
    if cached is not None:
        return _merge_live_mcp_tools(cached["tools"], config_path, include_unavailable)
    tools, _errors = available_mcp_tools(config_path=config_path, include_unavailable=include_unavailable)
    return tools


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


def _default_timeout(
    value: Optional[float],
    env_name: str = "GA_MCP_DISCOVERY_TIMEOUT",
    fallback: float = _MCP_DISCOVERY_TIMEOUT_DEFAULT,
) -> float:
    if value is not None:
        return float(value)
    try:
        return float(os.environ.get(env_name, fallback))
    except (TypeError, ValueError):
        return fallback


def _local_connect_concurrency() -> int:
    try:
        return max(
            1,
            int(os.environ.get("GA_MCP_LOCAL_CONNECT_CONCURRENCY", _MCP_LOCAL_CONNECT_CONCURRENCY_DEFAULT)),
        )
    except (TypeError, ValueError):
        return _MCP_LOCAL_CONNECT_CONCURRENCY_DEFAULT


def _mcp_stderr_log_max_bytes() -> int:
    try:
        return max(0, int(os.environ.get("GA_MCP_STDERR_LOG_MAX_BYTES", _MCP_STDERR_LOG_MAX_BYTES_DEFAULT)))
    except (TypeError, ValueError):
        return _MCP_STDERR_LOG_MAX_BYTES_DEFAULT


def _open_stderr_log(server_name: str):
    """Open a stdio server's stderr log, rotating it once it exceeds the cap."""
    _MCP_LOG_DIR.mkdir(parents=True, exist_ok=True)
    path = _MCP_LOG_DIR / f"{normalize_mcp_name(server_name)}.stderr.log"
    try:
        if path.exists() and path.stat().st_size >= _mcp_stderr_log_max_bytes():
            path.unlink()
    except OSError:
        pass
    return path.open("a", encoding="utf-8", errors="replace")


_NON_RETRYABLE_CONNECT_HINTS = (
    "unauthorized",
    "forbidden",
    "authentication",
    "auth required",
    "invalid api key",
    "invalid_api_key",
    "permission denied",
    "not found",
    "unsupported",
    "invalid argument",
    "401",
    "403",
    "404",
)
_RETRYABLE_CONNECT_HINTS = (
    "timeout",
    "timed out",
    "failed to initialize",
    "connect",
    "reset",
    "refused",
    "broken pipe",
    "unreachable",
    "unavailable",
    "temporarily",
    "disconnected",
    "network",
    "econnreset",
    "econnrefused",
    "readtimeout",
    "connecttimeout",
    "remoteprotocolerror",
    "too many requests",
    "429",
    "500",
    "502",
    "503",
    "504",
)


def _is_retryable_connect_error(error: str) -> bool:
    """Mirror Codex's retryable-initialize classification for remote servers."""
    text = str(error or "").lower()
    if not text:
        return False
    if any(hint in text for hint in _NON_RETRYABLE_CONNECT_HINTS):
        return False
    return any(hint in text for hint in _RETRYABLE_CONNECT_HINTS)


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


def _load_mcp_tools_cache_entry(
    cache_path: Path,
    signature: dict[str, Any],
    allow_stale: bool = False,
) -> Optional[dict[str, Any]]:
    """Return the cached discovery entry, or None when it does not match.

    ``allow_stale`` keeps a partial or aged result usable: callers that must not
    block a user turn return it as-is and schedule a background re-check, while
    the default keeps the original "partial results expire" contract.
    """
    try:
        data = json.loads(cache_path.read_text(encoding="utf-8"))
    except Exception:
        return None
    if data.get("signature") != signature:
        return None
    tools = data.get("tools")
    if not isinstance(tools, list):
        return None
    cached_at = data.get("cached_at")
    complete = bool(data.get("complete", True))
    if allow_stale:
        return {
            "tools": [dict(tool) for tool in tools if isinstance(tool, dict)],
            "complete": complete,
            "cached_at": cached_at if isinstance(cached_at, (int, float)) else None,
        }
    # Incomplete results (a server failed at discovery) expire after a short TTL
    # so a transient remote-server timeout does not permanently hide its tools.
    if not complete:
        if not isinstance(cached_at, (int, float)):
            return None
        if time.time() - cached_at > _MCP_TOOLS_CACHE_INCOMPLETE_TTL:
            return None
    return {
        "tools": [dict(tool) for tool in tools if isinstance(tool, dict)],
        "complete": complete,
        "cached_at": cached_at if isinstance(cached_at, (int, float)) else None,
    }


def _read_mcp_tools_cache(cache_path: Path, signature: dict[str, Any]) -> Optional[list[dict[str, Any]]]:
    entry = _load_mcp_tools_cache_entry(cache_path, signature)
    return None if entry is None else entry["tools"]


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
        stderr_log = _open_stderr_log(server_name)
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


# npx resolves the registry on every spawn even when the package is already in
# the npx cache. Measured on this machine, that round-trip is what made one
# stdio server cost 4-8s and spike to 16s on a slow network, versus a stable
# ~2s once the registry lookup is skipped. `--prefer-offline` keeps the cached
# package fast and still installs when it is genuinely missing, so a fresh
# machine keeps working. Applied by the host, not by mcp.json, because the
# server list is user config (and is gitignored).
_NPX_COMMANDS = frozenset({"npx", "npx.cmd", "npx.exe", "npx.ps1"})
_NPX_PREFER_OFFLINE_FLAG = "--prefer-offline"


def _prefer_offline_args(command: Any, args: Any) -> list[str]:
    resolved = [str(item) for item in (args or [])]
    if os.path.basename(str(command or "")).lower() not in _NPX_COMMANDS:
        return resolved
    if any(
        str(item).lower().startswith("--prefer-offline") or str(item).lower() == "--offline"
        for item in resolved
    ):
        return resolved
    return [_NPX_PREFER_OFFLINE_FLAG, *resolved]


def _normalize_server_config(server_config: dict[str, Any]) -> dict[str, Any]:
    cfg = dict(server_config)
    cfg_type = cfg.get("type")
    if cfg.get("url") and cfg_type in {"http", "streamable-http", "sse"} and not cfg.get("transport"):
        cfg["transport"] = cfg_type
    if cfg.get("command") and not cfg.get("transport"):
        cfg["transport"] = "stdio"
    if cfg.get("command"):
        cfg["args"] = _prefer_offline_args(cfg.get("command"), cfg.get("args"))
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
