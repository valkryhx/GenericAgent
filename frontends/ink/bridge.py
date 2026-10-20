"""JSONL bridge for the experimental React/Ink frontend.

This module intentionally stays independent from tuiapp_v2.py.  It exposes a
small stdin/stdout protocol so a Node/Ink process can drive GenericAgent without
embedding Python UI code.
"""

from __future__ import annotations

import argparse
import copy
import contextlib
import glob
import json
import os
import queue
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, TextIO


PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from genericagent.core.redaction import sanitize, redact_sensitive_text
from genericagent.workflow.store import build_artifact_ownership_index, preview_text
from genericagent.agents.wait import workflow_run_is_terminal
from genericagent.workflow.workspace import (
    WorkspacePathError,
    artifact_ownership_index,
    default_workspace_root,
    observed_artifact_paths,
    resolve_workspace_child,
    resolve_workspace_root,
    workspace_metadata,
    workspace_writes_with_writer,
)


def _configure_protocol_stdio() -> None:
    for name in ("stdin", "stdout", "stderr"):
        stream = getattr(sys, name, None)
        if stream is not None and hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


_configure_protocol_stdio()

Event = dict[str, Any]
EmitFn = Callable[[Event], None]
AgentFactory = Callable[[], Any]

_WORKFLOW_FINAL_PAYLOAD_MAX_BYTES = 64 * 1024
_WORKFLOW_HANDOFF_MAX_BYTES = 64 * 1024
# Product default for /workflow without --timeout. WorkflowRuntime itself still
# defaults to 10s for unit tests that construct it directly.
DEFAULT_WORKFLOW_TIMEOUT_SECONDS = 900.0
# Live per-job telemetry files written by running children.
_LIVE_TELEMETRY_RELATIVE = os.path.join("agents", "*", "live.json")
# How many trailing journal events the workflow detail payload carries. The panel
# derives everything from run/draft/progress; the tail is only for the raw view.
_WORKFLOW_DETAIL_EVENT_TAIL = 50


def _backend_log_path() -> str:
    path = os.path.join(PROJECT_DIR, "temp", "ink_bridge_backend.log")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


# Thread-safe backend log redirect.
# Concurrent workflow runtime + workflow_detail/progress used to open/close a log
# file per enter/exit via contextlib.redirect_stdout. Nested/racy exit restored a
# *closed* handle onto sys.stdout; child-agent threads then crashed with
# ValueError: I/O operation on closed file. Keep one long-lived log handle and
# refcount nested redirects under a lock.
_backend_io_lock = threading.RLock()
_backend_redirect_depth = 0
_backend_log_handle: TextIO | None = None
_backend_saved_stdout: TextIO | None = None
_backend_saved_stderr: TextIO | None = None


def _ensure_backend_log_handle() -> TextIO:
    global _backend_log_handle
    handle = _backend_log_handle
    if handle is None or getattr(handle, "closed", False):
        handle = open(_backend_log_path(), "a", encoding="utf-8", errors="replace")
        _backend_log_handle = handle
    return handle


@contextlib.contextmanager
def backend_output_redirect():
    """Redirect stdout/stderr to the bridge backend log (thread-safe, nestable)."""
    global _backend_redirect_depth, _backend_saved_stdout, _backend_saved_stderr
    with _backend_io_lock:
        if _backend_redirect_depth == 0:
            log = _ensure_backend_log_handle()
            _backend_saved_stdout = sys.stdout
            _backend_saved_stderr = sys.stderr
            sys.stdout = log
            sys.stderr = log
        _backend_redirect_depth += 1
    try:
        yield
    finally:
        with _backend_io_lock:
            _backend_redirect_depth = max(0, _backend_redirect_depth - 1)
            if _backend_redirect_depth == 0:
                if _backend_saved_stdout is not None:
                    sys.stdout = _backend_saved_stdout
                if _backend_saved_stderr is not None:
                    sys.stderr = _backend_saved_stderr
                _backend_saved_stdout = None
                _backend_saved_stderr = None
                # Do not close the log handle here: another thread may still be
                # printing after a concurrent enter/exit race window. Flush only.
                handle = _backend_log_handle
                if handle is not None and not getattr(handle, "closed", False):
                    try:
                        handle.flush()
                    except Exception:
                        pass


def encode_event(event: Event) -> str:
    return json.dumps(event, ensure_ascii=True, separators=(",", ":")) + "\n"


def _file_signature(path: str) -> tuple[int, int] | None:
    """Cheap change detector: (mtime_ns, size) or None when the file is absent."""
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return (int(stat.st_mtime_ns), int(stat.st_size))


def _read_json_object(path: str) -> dict[str, Any] | None:
    if not os.path.isfile(path):
        return None
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            payload = json.load(fh)
    except Exception:
        return None
    if not isinstance(payload, dict):
        return None
    return sanitize(payload)


def make_stdout_emitter(stdout: TextIO) -> EmitFn:
    lock = threading.Lock()

    def emit(event: Event) -> None:
        with lock:
            stdout.write(encode_event(sanitize(event)))
            stdout.flush()

    return emit


def default_agent_factory() -> Any:
    with backend_output_redirect():
        from genericagent.core.app import GenericAgent

    # Slice D3：启动时清理过期/超量的 GA 自产剪贴板图（不碰用户原图）
    try:
        from genericagent.media.image_gc import maybe_gc_ga_images_on_startup

        maybe_gc_ga_images_on_startup(quiet=True)
    except Exception:
        pass

    agent = GenericAgent()
    agent.inc_out = True
    agent.verbose = True
    return agent


try:
    from genericagent.core.context import compact_agent_context, replace_log_with_compact_history, should_auto_compact_agent
except Exception:  # pragma: no cover - compact core import failures are reported at call sites
    compact_agent_context = None
    replace_log_with_compact_history = None
    should_auto_compact_agent = None


# 自动压缩熔断器：连续失败这么多次后，本 session 停用自动压缩，避免摘要模型宕机时
# 每次用户请求都空打一次 API。对齐 Claude Code 的 MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES。
MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3


try:
    from frontends.terminal.continue_cmd import (
        extract_ui_messages as continue_extract,
        list_sessions as continue_list,
        reset_conversation as continue_reset,
        restore as continue_restore,
    )
except Exception:  # pragma: no cover - exercised only when optional frontend helpers fail to import
    continue_extract = None
    continue_list = None
    continue_reset = None
    continue_restore = None


try:
    from genericagent.core import transcripts as session_transcript
except Exception:  # pragma: no cover - transcript resume falls back to legacy replay
    session_transcript = None


class GenericAgentBridge:
    def __init__(
        self,
        agent_factory: AgentFactory = default_agent_factory,
        emit: EmitFn | None = None,
        workflow_root: str | os.PathLike[str] | None = None,
        workspace_root: str | os.PathLike[str] | None = None,
        workflow_runtime_factory: Callable[..., Any] | None = None,
        agent_control: Any | None = None,
    ) -> None:
        self.agent_factory = agent_factory
        # Artifacts must not land in GA's own source tree. ``os.getcwd()`` was
        # the repository root for a normal ``ga`` launch, so a generated report
        # appeared next to genericagent/core/app.py. The canonical root is the project's
        # gitignored temp/ directory.
        self.workspace_root = resolve_workspace_root(workspace_root or default_workspace_root())
        raw_emit = emit or make_stdout_emitter(sys.stdout)
        self.emit = lambda event: raw_emit(sanitize(event))
        with backend_output_redirect():
            self.agent = self.agent_factory()
        self._bind_agent(self.agent)
        self._task_seq = 0
        # 自动压缩熔断器：连续失败 N 次后本 session 停用自动压缩，避免摘要模型宕机
        # 时每次请求都空打一发（抄 Claude Code 的 MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES）。
        self._auto_compact_failures = 0
        self._auto_compact_disabled = False
        self._rewind_snapshots: dict[int, dict[str, Any]] = {}
        self._consume_thread: threading.Thread | None = None
        self._workflow_threads: dict[str, threading.Thread] = {}
        self._workflow_handoff_lock = threading.Lock()
        self._workflow_handed_off: set[str] = set()
        # Bridge-scoped session opt-in (``/workflow on|off``); per-turn opt-in is
        # still stateless and resolved from the request text.
        with backend_output_redirect():
            from genericagent.workflow.activation import WorkflowActivationState

        self._workflow_activation = WorkflowActivationState()
        with backend_output_redirect():
            from genericagent.agents.team_activation import TeamActivationState

        # Bridge-scoped agent-team session switch (``/team on|off``); ``GA_AGENT_TEAMS=1``
        # seeds it enabled. Per-turn activation stays stateless and is resolved from the
        # request text (CC has no team slash command; the session gate mirrors --agent-teams).
        self._team_activation = TeamActivationState()
        self._workflow_emitted_sequences: dict[str, set[int]] = {}
        self._mcp_watch_lock = threading.Lock()
        self._mcp_watch_thread: threading.Thread | None = None
        self.workflow_runtime_factory = workflow_runtime_factory
        with backend_output_redirect():
            from genericagent.workflow.controller import WorkflowController
            from genericagent.workflow.store import WorkflowStore

        # Project root the bridge reads shared state from (subagents/teams).
        # Overridable so tests can point the team view at a temp tree.
        self.project_dir = PROJECT_DIR
        self.workflow_store = WorkflowStore(root=workflow_root)
        self.workflow_controller = WorkflowController(store=self.workflow_store)
        if agent_control is None:
            with backend_output_redirect():
                from genericagent.core.control.control import UnifiedAgentControl
                from genericagent.core.control.process import ProcessSubagentAdapter
                from genericagent.core.control.workflow import WorkflowChildAdapter
                from genericagent.agents.manager import SubagentManager

            process_manager = SubagentManager(root_dir=PROJECT_DIR, python_executable=sys.executable)
            agent_control = UnifiedAgentControl(
                [
                    ProcessSubagentAdapter(process_manager),
                    # Scope the agent read model to the live session (CC keeps the
                    # workflow view per session). Unscoped, temp/sessions grew without
                    # bound and every workflow_detail/list/progress paid seconds of
                    # list_records+events_since work (the "Enter to view is laggy" bug).
                    WorkflowChildAdapter(
                        self.workflow_store,
                        self.workflow_controller,
                        session_id_provider=lambda: getattr(self.agent, "session_id", None),
                    ),
                ]
            )
        self.agent_control = agent_control
        self.agent_cursors: dict[str, int] = self._initial_agent_cursors()
        self._agent_event_ids: set[str] = set()
        self._agent_errors: dict[str, str] = {}
        self._agent_snapshot_fingerprint: str | None = None
        self._agent_thread = threading.Thread(target=self._run_agent, daemon=True, name="ga-ink-agent")
        self._agent_thread.start()

    def _bind_agent(self, agent: Any) -> None:
        """Attach the bridge-owned hooks to whichever agent currently runs turns.

        The model-visible ``workflow`` tool is dispatched from the agent's
        handler, which receives the agent object as ``parent``; the bridge owns
        run creation, so the launcher must live on the *current* agent. Every
        path that swaps ``self.agent`` (``__init__`` and ``new_session``) must
        call this, otherwise ``do_workflow`` degrades to
        "workflow tool launcher unavailable".
        """
        with backend_output_redirect():
            agent.inc_out = True
            agent.verbose = True
        agent.workflow_tool_launcher = self.workflow_launch_from_tool
        agent.hook_emit = self.emit
        # wait_agent(condition="workflow_terminal") resolves a run id through this
        # bridge-owned store; without it a run id is answered by an orphan subagent
        # state that never reaches a terminal status (GA defect A).
        agent.workflow_run_resolver = self._resolve_workflow_run_info
        # ask 档阻塞审批：把 emit 注入 agent.permission_runtime，使 dispatch 可发 permission_request
        try:
            runtime = getattr(agent, "permission_runtime", None)
            if runtime is not None and hasattr(runtime, "set_emit"):
                runtime.set_emit(self.emit)
        except Exception:
            pass

    def _run_agent(self) -> None:
        with backend_output_redirect():
            self.agent.run()

    def submit(self, text: str, display_text: str | None = None, images: list | None = None) -> int:
        text = str(text or "")
        if not text.strip() and not images:
            self.emit({"type": "error", "code": "empty_input", "message": "input is empty"})
            return -1
        if getattr(self.agent, "is_running", False) or self._is_consuming():
            self.emit({"type": "error", "code": "busy", "message": "agent is running"})
            return -1
        raw_text = text
        if not images:
            # CC-style opt-in: an explicit multi-agent request only *informs* the
            # main agent that it may author a workflow script with the ``workflow``
            # tool. GA no longer auto-plans; the main agent is the script author
            # (Claude Code's direct-JS model). A merely workflow-friendly task is
            # not opt-in, so a ``recommended`` verdict does nothing special.
            from genericagent.agents.team_activation import team_activation_reminder
            from genericagent.workflow.activation import workflow_opt_in_reminder

            stripped = raw_text.strip()
            lowered = stripped.lower()
            activation = self._workflow_activation.handle(text)
            team = self._team_activation.handle(text)

            # ``/workflow on|off`` and ``/team on|off`` are session switches, not requests:
            # answer them locally and never hand the marker to the model.
            if lowered == "/workflow on":
                self.emit({"type": "status", "status": "idle"})
                self.emit({"type": "system", "text": "Dynamic workflow session mode enabled: the agent will author a JS workflow script for multi-step requests."})
                return 0
            if lowered == "/workflow off":
                self.emit({"type": "status", "status": "idle"})
                self.emit({"type": "system", "text": "Dynamic workflow session mode disabled."})
                return 0
            if lowered == "/team on":
                self.emit({"type": "status", "status": "idle"})
                self.emit({"type": "system", "text": "Agent team session mode enabled: parallel / independent requests may stand up named teammates."})
                return 0
            if lowered == "/team off":
                self.emit({"type": "status", "status": "idle"})
                self.emit({"type": "system", "text": "Agent team session mode disabled."})
                return 0

            workflow_signals = set(activation.matched_signals)
            # An explicit *workflow* request (``/workflow <task>`` or "用 workflow") always
            # wins: the user asked for a scripted workflow, not a team. Everything else that
            # looks parallelizable gets the CC plan-approval nudge (adapted: a natural request
            # is enough), so the user never has to type team syntax.
            explicit_workflow = activation.action == "requested" and bool(
                {"slash_workflow", "workflow_mention"} & workflow_signals or activation.mode == "session"
            )
            team_wants = team.action in ("recommended", "requested")
            if explicit_workflow:
                # ``/workflow on`` (and the per-turn ``/workflow <task>`` prefix) are opt-in
                # markers, not part of the request: hand the model the task text without it.
                if lowered.startswith("/workflow"):
                    text = activation.task_text
                if text.strip():
                    text = workflow_opt_in_reminder(activation) + chr(10) + chr(10) + text
            elif team_wants and text.strip():
                text = team_activation_reminder(team) + chr(10) + chr(10) + text
            elif activation.action == "requested" and text.strip():
                text = workflow_opt_in_reminder(activation) + chr(10) + chr(10) + text
        if not self._auto_compact_if_needed(text):
            return -1
        self._task_seq += 1
        task_id = self._task_seq
        # The reminder is model-facing only: the transcript keeps the raw request.
        visible_text = str(display_text) if display_text is not None else raw_text
        self._rewind_snapshots[task_id] = self._snapshot_agent_state()
        self._rewind_snapshots[task_id]["text"] = visible_text
        self.emit({"type": "user", "taskId": task_id, "text": visible_text})
        self.emit({"type": "status", "status": "running", "taskId": task_id})
        try:
            display_queue = self.agent.put_task(text, source="user", images=images or [])
        except Exception as exc:
            self.emit({"type": "error", "code": "put_task_failed", "message": str(exc), "taskId": task_id})
            self.emit({"type": "status", "status": "idle", "taskId": task_id})
            return -1
        self._consume_thread = threading.Thread(
            target=self._consume_display_queue,
            args=(task_id, display_queue),
            daemon=True,
            name=f"ga-ink-consume-{task_id}",
        )
        self._consume_thread.start()
        return task_id

    def stop(self) -> None:
        try:
            from genericagent.hooks.runtime import run_hook_event
            if not getattr(self, "_hook_session_ended", False):
                run_hook_event(self.agent, 'SessionEnd', {'reason': 'exit'}, emit=self.emit)
                self._hook_session_ended = True
        except Exception:
            pass
        if getattr(self.agent, "is_running", False) or self._is_consuming():
            try:
                with backend_output_redirect():
                    self.agent.abort()
            finally:
                self.emit({"type": "status", "status": "stopping"})
            return
        stopped_workflow = False
        for run_id, thread in list(self._workflow_threads.items()):
            if not thread.is_alive():
                continue
            try:
                with backend_output_redirect():
                    run = self.workflow_store.load_run(run_id)
                if run.status in {"running", "interrupted"}:
                    stopped_workflow = self.workflow_stop(run_id, reason="stopped from Ink bridge") or stopped_workflow
            except Exception:
                continue
        if stopped_workflow:
            self.emit({"type": "status", "status": "stopping"})
        else:
            self.emit({"type": "status", "status": "idle"})

    def new_session(self) -> None:
        if getattr(self.agent, "is_running", False) or self._is_consuming():
            self.emit({"type": "error", "code": "busy", "message": "agent is running"})
            return
        try:
            from genericagent.hooks.runtime import run_hook_event
            run_hook_event(self.agent, 'SessionEnd', {'reason': 'clear'}, emit=self.emit)
        except Exception:
            pass
        try:
            with backend_output_redirect():
                self.agent.abort()
        except Exception:
            pass
        with backend_output_redirect():
            self.agent = self.agent_factory()
        self._bind_agent(self.agent)
        self._task_seq = 0
        self._rewind_snapshots.clear()
        self._consume_thread = None
        self._agent_thread = threading.Thread(target=self._run_agent, daemon=True, name="ga-ink-agent")
        self._agent_thread.start()
        self.emit({"type": "history_replace", "messages": []})
        self.emit({"type": "system", "text": "Started a new session."})
        self.emit({"type": "status", "status": "idle"})

    def mcp_status(self) -> None:
        try:
            with backend_output_redirect():
                from genericagent.mcp.runtime import mcp_status_snapshot

                payload = mcp_status_snapshot()
            self.emit({"type": "mcp_status", **payload})
        except Exception as exc:
            self.emit({"type": "error", "code": "mcp_status_failed", "message": str(exc)})

    def start_mcp_status_watch(self) -> None:
        """Publish MCP connection/tool discovery progress without blocking JSONL commands."""
        with self._mcp_watch_lock:
            if self._mcp_watch_thread is not None and self._mcp_watch_thread.is_alive():
                return
            self._mcp_watch_thread = threading.Thread(
                target=self._watch_mcp_status,
                daemon=True,
                name="ga-ink-mcp-status",
            )
            self._mcp_watch_thread.start()

    def _watch_mcp_status(self) -> None:
        try:
            with backend_output_redirect():
                from genericagent.mcp.runtime import mcp_status_snapshot, start_background_discovery

                start_background_discovery()
            recovery_done = False
            while True:
                with backend_output_redirect():
                    payload = mcp_status_snapshot()
                loading = bool(payload.get("loading"))
                self.emit({"type": "mcp_progress", **payload, "loading": loading})
                if loading:
                    threading.Event().wait(0.25)
                    continue
                failed = [
                    server
                    for server in payload.get("servers", [])
                    if server.get("status") == "failed"
                ]
                if failed and not recovery_done:
                    # One bounded self-heal: retry only the servers that failed
                    # rather than re-running discovery for every server. A full
                    # second pass used to double the "initializing" window for
                    # every user because one flaky server had failed.
                    recovery_done = True
                    with backend_output_redirect():
                        from genericagent.mcp.runtime import reconnect_mcp_server
                        for server in failed:
                            try:
                                reconnect_mcp_server(str(server.get("name") or ""))
                            except Exception:
                                pass
                    threading.Event().wait(0.25)
                    continue
                return
        except Exception as exc:
            self.emit({
                "type": "mcp_progress",
                "config_path": "",
                "servers": [],
                "tools": [],
                "errors": {"startup": str(exc)},
                "loading": False,
                "discovery_running": False,
                "discovery_complete": False,
            })

    def mcp_reconnect(self, server_name: str) -> None:
        try:
            with backend_output_redirect():
                from genericagent.mcp.runtime import reconnect_mcp_server

                result = reconnect_mcp_server(str(server_name or ""))
            status = result.get("server", {}).get("status", "unknown")
            self.emit({"type": "system", "text": f"MCP server {server_name} reconnect: {status}"})
        except Exception as exc:
            self.emit({"type": "error", "code": "mcp_reconnect_failed", "message": str(exc)})
        self.mcp_status()

    def mcp_enable(self, server_name: str) -> None:
        try:
            with backend_output_redirect():
                from genericagent.mcp.runtime import enable_mcp_server

                result = enable_mcp_server(str(server_name or ""))
            status = result.get("server", {}).get("status", "unknown")
            self.emit({"type": "system", "text": f"MCP server {server_name} enabled: {status}"})
        except Exception as exc:
            self.emit({"type": "error", "code": "mcp_enable_failed", "message": str(exc)})
        self.mcp_status()

    def mcp_disable(self, server_name: str) -> None:
        try:
            with backend_output_redirect():
                from genericagent.mcp.runtime import disable_mcp_server

                result = disable_mcp_server(str(server_name or ""))
            status = result.get("server", {}).get("status", "unknown")
            self.emit({"type": "system", "text": f"MCP server {server_name} disabled: {status}"})
        except Exception as exc:
            self.emit({"type": "error", "code": "mcp_disable_failed", "message": str(exc)})
        self.mcp_status()

    def model_status(self) -> None:
        try:
            with backend_output_redirect():
                if hasattr(self.agent, "list_llm_descriptors"):
                    models = self.agent.list_llm_descriptors()
                else:
                    models = [
                        {"index": int(index), "name": str(name), "current": bool(current)}
                        for index, name, current in self.agent.list_llms()
                    ]
            self.emit({"type": "model_status", "models": models})
        except Exception as exc:
            self.emit({"type": "error", "code": "model_status_failed", "message": str(exc)})

    def model_switch(self, selector: str, reasoning_effort: str | None = None) -> None:
        if getattr(self.agent, "is_running", False) or self._is_consuming():
            self.emit({"type": "error", "code": "busy", "message": "agent is running"})
            return
        try:
            from genericagent.hooks.runtime import run_hook_event
            target = str(selector or "")
            pre = run_hook_event(self.agent, 'PreModelSwitch', {
                'model': target,
                'reasoning_effort': reasoning_effort,
            }, emit=self.emit)
            if pre.blocked or pre.permission_decision == 'deny':
                self.emit({"type": "model_switch_result", "ok": False,
                           "message": pre.reason or "model switch blocked by PreModelSwitch hook"})
                return
            with backend_output_redirect():
                # Keep the bridge compatible with lightweight/legacy agent
                # adapters that only implement ``select_llm(selector)``.
                # The optional effort is only part of the new protocol, so do
                # not force an extra keyword into the old call shape when the
                # user is merely switching models.
                if reasoning_effort is None:
                    result = self.agent.select_llm(str(selector or ""))
                else:
                    result = self.agent.select_llm(
                        str(selector or ""),
                        reasoning_effort=reasoning_effort,
                    )
            if result.get("ok"):
                effort = result.get("reasoning_effort")
                suffix = f" ({effort})" if effort else ""
                post = run_hook_event(self.agent, 'PostModelSwitch', {
                    'model': result.get('name'),
                    'reasoning_effort': effort,
                }, emit=self.emit)
                message = f"Set model to {result.get('name')}{suffix}"
                if post.context_text:
                    message += chr(10) * 2 + post.context_text
                self.emit({"type": "model_switch_result", "ok": True, "message": message})
            else:
                self.emit({"type": "model_switch_result", "ok": False, "message": str(result.get("message") or "model switch failed")})
        except Exception as exc:
            self.emit({"type": "error", "code": "model_switch_failed", "message": str(exc)})
        self.model_status()

    def permission_status(self) -> None:
        '''上报当前权限档与可选档位列表，供 Ink 面板渲染。'''
        try:
            with backend_output_redirect():
                from genericagent.core.permissions.policy import (
                    DEFAULT_PERMISSION_MODE,
                    PERMISSION_MODES,
                    normalize_permission_mode,
                )

                current = normalize_permission_mode(getattr(self.agent, "permission_mode", None))
            self.emit(
                {
                    "type": "permission_status",
                    "mode": current,
                    "default": DEFAULT_PERMISSION_MODE,
                    "modes": list(PERMISSION_MODES),
                }
            )
        except Exception as exc:
            self.emit({"type": "error", "code": "permission_status_failed", "message": str(exc)})

    def permission_switch(self, mode: str, *, persist: bool = False) -> None:
        '''切换主会话权限档（read_only / ask / full_access）。

        允许运行中切换：set_permission_mode 会同步更新 live handler，下一次
        dispatch 立即按新档评估。切换后回发 permission_status 让 UI 收敛。

        persist 预留给后续「记住选择」的持久化设置层；当前 MVP 为 session-only，
        接受该参数但不写盘（避免协议破坏），切换只影响本 session。
        '''
        _ = persist  # MVP：session-only，不做持久化
        try:
            with backend_output_redirect():
                applied = self.agent.set_permission_mode(str(mode or ""))
            self.emit(
                {
                    "type": "permission_switch_result",
                    "ok": True,
                    "mode": applied,
                }
            )
        except Exception as exc:
            self.emit({"type": "error", "code": "permission_switch_failed", "message": str(exc)})
        self.permission_status()

    def permission_response(self, request_id: str, decision: str) -> None:
        '''UI 对 permission_request 的应答：accept | deny。'''
        try:
            runtime = getattr(self.agent, "permission_runtime", None)
            if runtime is None:
                self.emit(
                    {
                        "type": "error",
                        "code": "permission_response_failed",
                        "message": "permission_runtime unavailable",
                    }
                )
                return
            ok = bool(runtime.resolve(str(request_id or ""), decision))
            if not ok:
                self.emit(
                    {
                        "type": "error",
                        "code": "permission_response_unknown",
                        "message": f"unknown or settled requestId: {request_id}",
                    }
                )
        except Exception as exc:
            self.emit({"type": "error", "code": "permission_response_failed", "message": str(exc)})

    def skill_status(self, search_roots: list[str] | None = None) -> None:
        try:
            with backend_output_redirect():
                from genericagent.core.skills import skill_status_rows

                skills = skill_status_rows(search_roots=search_roots)
            self.emit(
                {
                    "type": "skill_status",
                    "skills": [
                        {
                            "name": str(skill.get("name") or ""),
                            "description": str(skill.get("description") or ""),
                            "source": str(skill.get("source") or ""),
                            "path": str(skill.get("path") or ""),
                            "state": str(skill.get("state") or "on"),
                            "alwaysOnChars": int(skill.get("always_on_chars") or 0),
                            "onInvokeChars": int(skill.get("on_invoke_chars") or 0),
                            "modelVisible": bool(skill.get("model_visible")),
                            "userInvocable": bool(skill.get("user_invocable")),
                        }
                        for skill in skills
                    ],
                }
            )
        except Exception as exc:
            self.emit({"type": "error", "code": "skill_status_failed", "message": str(exc)})

    def skill_invoke(self, skill_name: str, args: str = "", search_roots: list[str] | None = None) -> int:
        if getattr(self.agent, "is_running", False) or self._is_consuming():
            self.emit({"type": "error", "code": "busy", "message": "agent is running"})
            return -1
        try:
            with backend_output_redirect():
                from genericagent.core.skills import load_skill_content

                loaded = load_skill_content(
                    str(skill_name or ""),
                    search_roots=search_roots,
                    args=str(args or ""),
                    invocation="user",
                )
        except KeyError as exc:
            self.emit({"type": "error", "code": "skill_not_found", "message": str(exc)})
            return -1
        except Exception as exc:
            self.emit({"type": "error", "code": "skill_invoke_failed", "message": str(exc)})
            return -1

        request = str(args or "").strip()
        fallback_request = f"Use the {loaded.get('name')} skill."
        prompt = (
            f'[SYSTEM] The user invoked skill "{loaded.get("name")}" via slash command.\n'
            "You must follow the loaded SKILL.md instructions.\n\n"
            "<skill>\n"
            f"{loaded.get('content', '')}\n"
            "</skill>\n\n"
            "<arguments>\n"
            f"{request}\n"
            "</arguments>\n\n"
            "User request:\n"
            f"{request or fallback_request}"
        )
        visible = f"/{loaded.get('name')} {request}".rstrip()
        return self.submit(prompt, display_text=visible)

    def compact(self, instructions: str = "") -> None:
        if getattr(self.agent, "is_running", False) or self._is_consuming():
            self.emit({"type": "error", "code": "busy", "message": "agent is running"})
            return
        if compact_agent_context is None:
            self.emit({"type": "error", "code": "compact_unavailable", "message": "/compact is unavailable"})
            return
        self.emit({"type": "status", "status": "running"})
        self.emit({"type": "activity", "label": "Compacting conversation"})
        try:
            from genericagent.hooks.runtime import run_hook_event
            pre = run_hook_event(self.agent, 'PreCompact', {
                'trigger': 'manual',
                'custom_instructions': str(instructions or ''),
            }, emit=self.emit)
            if pre.blocked:
                self.emit({"type": "error", "code": "compact_hook_blocked", "message": pre.reason or "PreCompact hook blocked compaction"})
                return
            with backend_output_redirect():
                result = compact_agent_context(self.agent, instructions=str(instructions or ""))
            if not result.ok:
                self.emit({"type": "local_command_output", "text": f"Compact failed: {result.message}"})
                return
            post = run_hook_event(self.agent, 'PostCompact', {
                'trigger': 'manual',
                'compact_summary': result.message,
            }, emit=self.emit)
            self._replace_compact_log()
            self._record_compact_transcript(result.message)
            self._rewind_snapshots.clear()
            text = result.message
            if post.context_text:
                text += chr(10) * 2 + '[Hook context] ' + post.context_text
            # 成功结果只走 history_replace：同一文案再发 local_command_output 会在
            # Ink Static 滚动区留下一份，history_replace 后再打一份 → 用户看到重复。
            # 失败仍用 local_command_output（无 history_replace）。
            self.emit({"type": "history_replace", "messages": [
                {"role": "system", "text": text},
            ]})
        finally:
            self.emit({"type": "activity", "label": None})
            self.emit({"type": "status", "status": "idle"})

    def workflow_draft(self, script: str) -> str:
        if getattr(self.agent, "is_running", False) or self._is_consuming():
            self.emit({"type": "error", "code": "busy", "message": "agent is running"})
            return ""
        try:
            session_id = str(getattr(self.agent, "session_id", "") or "ink-session")
            with backend_output_redirect():
                run = self.workflow_controller.create_draft(session_id=session_id, script=str(script or ""))
                run = self.workflow_controller.request_approval(run.run_id)
            self._emit_agent_read_model()
            self.emit({"type": "workflow_draft", "run": self._workflow_run_payload(run)})
            self._emit_workflow_events(run.run_id)
            return run.run_id
        except Exception as exc:
            self.emit({"type": "error", "code": "workflow_draft_failed", "message": str(exc)})
            return ""

    def workflow_approve(self, run_id: str, *, args: Any = None, timeout_seconds: float | None = None) -> bool:
        if getattr(self.agent, "is_running", False) or self._is_consuming():
            self.emit({"type": "error", "code": "busy", "message": "agent is running"})
            return False
        run_id = str(run_id or "")
        if not run_id:
            self.emit({"type": "error", "code": "workflow_bad_run_id", "message": "workflow runId is required"})
            return False
        try:
            with backend_output_redirect():
                run = self.workflow_controller.approve(run_id)
            self._emit_agent_read_model()
            self.emit({"type": "workflow_run", "run": self._workflow_run_payload(run)})
            self._emit_workflow_events(run.run_id)
        except Exception as exc:
            self.emit({"type": "error", "code": "workflow_approve_failed", "message": str(exc)})
            return False
        thread = threading.Thread(
            target=self._run_workflow_runtime,
            args=(run.run_id, args, self._effective_workflow_timeout(timeout_seconds), None),
            daemon=True,
            name=f"ga-ink-workflow-{run.run_id}",
        )
        self._workflow_threads[run.run_id] = thread
        thread.start()
        return True

    def workflow_resume(self, run_id: str, *, args: Any = None, timeout_seconds: float | None = None) -> str:
        if getattr(self.agent, "is_running", False) or self._is_consuming():
            self.emit({"type": "error", "code": "busy", "message": "agent is running"})
            return ""
        source_run_id = str(run_id or "")
        if not source_run_id:
            self.emit({"type": "error", "code": "workflow_bad_run_id", "message": "workflow runId is required"})
            return ""
        try:
            with backend_output_redirect():
                from genericagent.workflow.models import WorkflowEvent

                source = self.workflow_store.load_run(source_run_id)
                if source.status not in {"succeeded", "degraded", "failed", "killed", "interrupted"}:
                    raise ValueError(f"cannot resume workflow {source_run_id} from {source.status}")
                resumed = self.workflow_controller.create_draft(session_id=source.session_id, script=source.script)
                resumed.status = "running"
                resumed.metadata["resumeFromRunId"] = source_run_id
                self.workflow_store.save_run(resumed)
                self.workflow_store.append_event(
                    resumed,
                    WorkflowEvent(
                        run_id=resumed.run_id,
                        session_id=resumed.session_id,
                        event_type="workflow_started",
                        sequence=0,
                        payload={"resumeFromRunId": source_run_id},
                    ),
                )
            self._emit_agent_read_model()
            self.emit({"type": "workflow_run", "run": self._workflow_run_payload(resumed)})
        except Exception as exc:
            self.emit({"type": "error", "code": "workflow_resume_failed", "message": str(exc)})
            return ""
        thread = threading.Thread(
            target=self._run_workflow_runtime,
            args=(resumed.run_id, args, self._effective_workflow_timeout(timeout_seconds), source_run_id),
            daemon=True,
            name=f"ga-ink-workflow-{resumed.run_id}",
        )
        self._workflow_threads[resumed.run_id] = thread
        thread.start()
        return resumed.run_id

    def workflow_save(self, run_id: str, *, name: str | None = None, overwrite: bool = False) -> bool:
        """Save a run's script as a named workflow (CC m1906 "save as").

        CC evidence (m1906_0xe19cc02.js): the default name comes from the
        script's ``meta.name`` (falling back to a slug of the summary), and a
        name collision asks the user to press Enter again to overwrite. GA has
        no modal dialog, so the second press is the explicit ``overwrite`` flag
        (``/workflow save <runId> <name> --overwrite``); without it a collision
        is reported as ``workflow_name_exists`` and nothing is written.
        """
        run_id = str(run_id or "")
        if not run_id:
            self.emit({"type": "error", "code": "workflow_bad_run_id", "message": "workflow runId is required"})
            return False
        try:
            with backend_output_redirect():
                run = self.workflow_store.load_run(run_id)
                script = str(run.script or "")
                if not script.strip():
                    raise ValueError("workflow has no script to save")
                metadata = run.metadata if isinstance(run.metadata, dict) else {}
                requested = str(name or "").strip() or str(metadata.get("workflowName") or "").strip() or run_id
                record = self.workflow_store.save_named_workflow(
                    requested,
                    script,
                    source_run_id=run_id,
                    description=str(metadata.get("workflowTaskType") or "").strip() or None,
                    overwrite=bool(overwrite),
                )
        except FileExistsError as exc:
            slug = exc.args[0] if exc.args else "workflow"
            self.emit({
                "type": "error",
                "code": "workflow_name_exists",
                "message": f"workflow '{slug}' already exists; save again with --overwrite to replace it",
            })
            return False
        except Exception as exc:
            self.emit({"type": "error", "code": "workflow_save_failed", "message": str(exc)})
            return False
        self.emit({"type": "workflow_saved", "workflow": record, "overwritten": bool(overwrite)})
        self._emit_named_workflows()
        return True

    def workflow_launch_from_tool(
        self,
        *,
        script: str | None = None,
        script_path: str | None = None,
        name: str | None = None,
        args: Any = None,
        resume_from_run_id: str | None = None,
        timeout_seconds: float | None = None,
    ) -> dict[str, Any]:
        """Launch a workflow from the model-visible workflow tool.

        This method is called from inside the main agent's tool dispatch, when
        ``agent.is_running`` is true. It therefore intentionally does not use
        the busy guard shared by the interactive workflow commands.
        """
        requested_script = str(script).strip() if isinstance(script, str) and str(script).strip() else ""
        requested_path = str(script_path).strip() if isinstance(script_path, str) and str(script_path).strip() else ""
        requested_name = str(name).strip() if isinstance(name, str) and str(name).strip() else ""
        if sum(bool(value) for value in (requested_script, requested_path, requested_name)) > 1:
            return {"status": "error", "error": "workflow accepts exactly one of script, scriptPath, or name"}
        resume_id = str(resume_from_run_id).strip() if isinstance(resume_from_run_id, str) and str(resume_from_run_id).strip() else ""
        try:
            resume_run = None
            if resume_id:
                resume_run = self.workflow_store.load_run(resume_id)
                if resume_run.status not in {"succeeded", "degraded", "failed", "killed", "cancelled", "interrupted"}:
                    raise ValueError(f"cannot resume workflow {resume_id} from {resume_run.status}")
            source_kind = ""
            source_name = ""
            if requested_name:
                record = self.workflow_store.load_named_workflow(requested_name)
                workflow_script = str(record.get("script") or "")
                source_kind = "named"
                source_name = str(record.get("name") or requested_name)
            elif requested_path:
                workflow_script = self._read_workflow_tool_script_path(requested_path)
                source_kind = "scriptPath"
                source_name = Path(requested_path).name
            elif requested_script:
                workflow_script = str(script)
                source_kind = "inline"
            elif resume_run is not None:
                workflow_script = str(resume_run.script or "")
                source_kind = "resume"
                source_name = str((resume_run.metadata or {}).get("workflowName") or "")
            else:
                raise ValueError("workflow requires script, scriptPath, name, or resumeFromRunId")
            if not workflow_script.strip():
                raise ValueError("workflow script is empty")
            from genericagent.workflow.script_validation import validate_rendered_workflow_script

            preflight = validate_rendered_workflow_script(workflow_script)
            if not preflight.get("ok"):
                raise ValueError("Invalid workflow script: " + str(preflight.get("error") or "workflow script validation failed"))
            meta = preflight.get("meta") if isinstance(preflight.get("meta"), dict) else {}
            workflow_name = str(meta.get("name") or source_name or "dynamic-workflow")
            summary = str(meta.get("description") or "")
            session_id = str(getattr(self.agent, "session_id", "") or "ink-session")
            with backend_output_redirect():
                run = self.workflow_controller.create_draft(session_id=session_id, script=workflow_script)
                run.status = "running"
                run.metadata.update({
                    "workflowName": workflow_name,
                    "workflowSummary": summary,
                    "workflowTaskType": "dynamic_workflow",
                    "launchedBy": "workflow_tool",
                    "scriptSource": source_kind,
                    # ``_workflow_handoff_payload`` quotes the originating request
                    # back to the main agent; the model-authored script has no
                    # separate taskText draft to read it from.
                    "originalTask": str(meta.get("description") or summary or ""),
                })
                if requested_name:
                    run.metadata["namedWorkflow"] = source_name
                if resume_id:
                    run.metadata["resumeFromRunId"] = resume_id
                if not self._assign_run_workspace(run):
                    run.metadata.update(workspace_metadata(self.workspace_root))
                self.workflow_store.save_run(run)
                from genericagent.workflow.models import WorkflowEvent

                self.workflow_store.append_event(
                    run,
                    WorkflowEvent(
                        run_id=run.run_id,
                        session_id=run.session_id,
                        event_type="workflow_started",
                        sequence=0,
                        payload={
                            "launchedBy": "workflow_tool",
                            "scriptSource": source_kind,
                            "workflowName": workflow_name,
                            "resumeFromRunId": resume_id or None,
                        },
                    ),
                )
        except FileNotFoundError as exc:
            return {"status": "error", "error": f"workflow source not found: {exc}"}
        except Exception as exc:
            return {"status": "error", "error": redact_sensitive_text(str(exc))}

        self.emit({"type": "workflow_run", "run": self._workflow_run_payload(run)})
        self._emit_workflow_events(run.run_id)
        try:
            effective_timeout = self._effective_workflow_timeout(timeout_seconds)
        except Exception as exc:
            return {"status": "error", "error": redact_sensitive_text(str(exc))}
        thread = threading.Thread(
            target=self._run_workflow_runtime,
            args=(run.run_id, args, effective_timeout, resume_id or None),
            daemon=True,
            name=f"ga-ink-workflow-{run.run_id}",
        )
        self._workflow_threads[run.run_id] = thread
        thread.start()
        artifact_dir = os.fspath(run.artifact_dir or "")
        return {
            "status": "async_launched",
            "taskId": f"local_workflow_{run.run_id}",
            "taskType": "local_workflow",
            "runId": run.run_id,
            "workflowName": workflow_name,
            "summary": summary,
            "transcriptDir": os.path.join(artifact_dir, "agents"),
            "scriptPath": os.path.join(artifact_dir, "script.js"),
            "workspacePath": str((run.metadata or {}).get("workspacePath") or self.workspace_root),
            "message": (
                "Workflow launched in background. You will be notified when it completes; "
                "do not block waiting (no wait_agent) -- report the launch and end your turn. "
                "Use /workflows to monitor."
            ),
        }

    def _read_workflow_tool_script_path(self, raw_path: str) -> str:
        roots = [self.workspace_root]
        store_root = Path(self.workflow_store.root)
        if store_root.exists():
            roots.append(store_root)
        for root in roots:
            try:
                path = resolve_workspace_child(raw_path, root)
            except (WorkspacePathError, OSError, ValueError):
                continue
            if path.is_file():
                return path.read_text(encoding="utf-8", errors="replace")
        raise FileNotFoundError(f"workflow scriptPath not found or outside workspace: {raw_path}")

    def workflow_run_named(self, name: str, *, args: Any = None, timeout_seconds: float | None = None) -> str:
        """Start a fresh run from a saved named workflow (CC Workflow({name, args}))."""
        if getattr(self.agent, "is_running", False) or self._is_consuming():
            self.emit({"type": "error", "code": "busy", "message": "agent is running"})
            return ""
        requested = str(name or "").strip()
        if not requested:
            self.emit({"type": "error", "code": "workflow_bad_name", "message": "workflow name is required"})
            return ""
        try:
            with backend_output_redirect():
                from genericagent.workflow.models import WorkflowEvent

                record = self.workflow_store.load_named_workflow(requested)
                session_id = str(getattr(self.agent, "session_id", "") or "ink-session")
                run = self.workflow_controller.create_draft(session_id=session_id, script=str(record.get("script") or ""))
                run.status = "running"
                run.metadata["workflowName"] = str(record.get("name") or requested)
                run.metadata["namedWorkflow"] = str(record.get("name") or requested)
                self.workflow_store.save_run(run)
                self.workflow_store.append_event(
                    run,
                    WorkflowEvent(
                        run_id=run.run_id,
                        session_id=run.session_id,
                        event_type="workflow_started",
                        sequence=0,
                        payload={"namedWorkflow": run.metadata["namedWorkflow"]},
                    ),
                )
            self._emit_agent_read_model()
            self.emit({"type": "workflow_run", "run": self._workflow_run_payload(run)})
        except FileNotFoundError:
            self.emit({"type": "error", "code": "workflow_name_not_found", "message": f"no saved workflow named '{requested}'"})
            return ""
        except Exception as exc:
            self.emit({"type": "error", "code": "workflow_run_named_failed", "message": str(exc)})
            return ""
        thread = threading.Thread(
            target=self._run_workflow_runtime,
            args=(run.run_id, args, self._effective_workflow_timeout(timeout_seconds), None),
            daemon=True,
            name=f"ga-ink-workflow-{run.run_id}",
        )
        self._workflow_threads[run.run_id] = thread
        thread.start()
        return run.run_id

    def workflow_named_list(self) -> None:
        try:
            self._emit_named_workflows()
        except Exception as exc:
            self.emit({"type": "error", "code": "workflow_named_list_failed", "message": str(exc)})

    def _emit_named_workflows(self) -> None:
        self.emit({"type": "named_workflows", "workflows": self.workflow_store.list_named_workflows()})

    def workflow_list(self) -> None:
        try:
            runs = self._list_workflow_runs()
            # CC parity / perf: the workflow list+detail panels are fed by these
            # events, not by the generic agent read model. Emitting it here cost
            # ~1.3 s + ~1.6 MB per keypress (list_records/events_since over the
            # whole store), which is what made "Enter to view" feel frozen.
            self.emit({
                "type": "workflow_runs",
                "runs": [self._workflow_run_summary(run) for run in runs],
                # CC m1906 lists saved named workflows next to the run history;
                # GA surfaces them as names so the list panel can hint
                # ``/workflow run <name>``.
                "savedWorkflows": [str(item.get("name") or "") for item in self.workflow_store.list_named_workflows()],
            })
        except Exception as exc:
            self.emit({"type": "error", "code": "workflow_list_failed", "message": str(exc)})

    def hooks_status(self) -> None:
        """Emit the CC-style /hooks inventory without leaking huge audit payloads."""
        try:
            from genericagent.hooks.runtime import hook_runtime_for_agent
            runtime = hook_runtime_for_agent(self.agent)
            self.emit({"type": "hooks_status", **runtime.status_snapshot(recent=30)})
        except Exception as exc:
            self.emit({"type": "error", "code": "hooks_status_failed", "message": str(exc)})

    def team_list(self) -> None:
        """Emit the CC-style team roster + shared task board (GA ``/team``, ``/agents``).

        Mirrors the ``/workflows`` list panel: a lightweight payload the frontend
        renders as an overlay, not the generic agent read model.
        """
        try:
            self.emit({"type": "team_roster", **self._team_roster_payload()})
        except Exception as exc:
            self.emit({"type": "error", "code": "team_list_failed", "message": str(exc)})

    def _team_roster_payload(self) -> dict[str, Any]:
        with backend_output_redirect():
            from genericagent.agents.registry import SubagentRegistry
            from genericagent.agents.tasks import SubagentTaskStore
            from genericagent.agents.team import SubagentTeam, team_name_for

        project_dir = getattr(self, "project_dir", PROJECT_DIR)
        registry = SubagentRegistry(Path(project_dir) / "temp" / "subagents")
        team_name = team_name_for(self._current_session_id())
        team = SubagentTeam(registry, "/root", team_name=team_name)
        view = team.view()
        store = SubagentTaskStore(project_dir, list_id=team_name)
        try:
            tasks = store.list_tasks()
        except Exception:
            tasks = []
        by_owner: dict[str, list[dict[str, Any]]] = {}
        for task in tasks:
            by_owner.setdefault(str(task.get("owner") or ""), []).append(task)
        lead_id = str(view["leadAgentId"])
        members: list[dict[str, Any]] = []
        for member in view["members"]:
            agent_id = str(member.get("agentId") or "")
            owned = by_owner.get(agent_id, [])
            current = next((t for t in owned if t.get("status") == "in_progress"), None)
            members.append(
                {
                    "agentId": agent_id,
                    "name": member.get("name"),
                    "status": member.get("status"),
                    "parentPath": member.get("parentPath"),
                    "runId": member.get("runId"),
                    "role": "lead" if agent_id == lead_id else "teammate",
                    "taskCount": len(owned),
                    "currentTask": current.get("subject") if current else None,
                }
            )
        return {
            "team": {
                "teamName": view["teamName"],
                "leadAgentId": lead_id,
                "leadName": view["leadName"],
                "memberCount": view["memberCount"],
            },
            "members": members,
            "tasks": tasks,
        }

    def workflow_detail(self, run_id: str) -> None:
        try:
            with backend_output_redirect():
                run = self.workflow_store.load_run(str(run_id or ""))
                # The panel is built from run/draft/progress (workflowOverviewFromDetail
                # never reads ``events``); CC's detail view reads agent transcripts on
                # demand (m1460). Ship only a bounded tail instead of the whole journal
                # so a long run cannot push a multi-hundred-KB frame on every Enter.
                events = self.workflow_store.replay_events(run.run_id)[-_WORKFLOW_DETAIL_EVENT_TAIL:]
                draft = self._workflow_artifact_payload(run, run.metadata.get("workflowDraftRef"))
                progress = self._workflow_artifact_payload(run, "workflow-progress.json")
            self.emit(
                {
                    "type": "workflow_detail",
                    "run": self._workflow_run_payload(run),
                    "script": run.script,
                    "events": [event.to_dict() for event in events],
                    "draft": draft,
                    "progress": progress,
                }
            )
        except Exception as exc:
            self.emit({"type": "error", "code": "workflow_detail_failed", "message": str(exc)})

    def workflow_progress(self, run_id: str) -> None:
        try:
            with backend_output_redirect():
                run = self.workflow_store.load_run(str(run_id or ""))
                progress = self._workflow_artifact_payload(run, "workflow-progress.json")
                if progress is None:
                    # A run created before the first job runs has no snapshot yet
                    # (or was written by an older build). Publish one from the
                    # run itself rather than reporting a failure for a healthy
                    # run: the caller asked for progress, and the run *is* the
                    # source of truth for it.
                    self.workflow_store.write_workflow_progress(run)
                    progress = self._workflow_artifact_payload(run, "workflow-progress.json")
            if progress is None:
                self.emit({"type": "error", "code": "workflow_progress_missing", "message": "workflow progress is not available"})
                return
            self.emit({"type": "workflow_progress", "progress": progress})
        except Exception as exc:
            self.emit({"type": "error", "code": "workflow_progress_failed", "message": str(exc)})

    def workflow_deny(self, run_id: str, *, reason: str = "") -> bool:
        run_id = str(run_id or "")
        if not run_id:
            self.emit({"type": "error", "code": "workflow_bad_run_id", "message": "workflow runId is required"})
            return False
        try:
            with backend_output_redirect():
                run = self.workflow_controller.deny(run_id, reason=reason or "denied from Ink bridge")
            self._emit_agent_read_model()
            self.emit({"type": "workflow_run", "run": self._workflow_run_payload(run)})
            self._emit_workflow_events(run.run_id)
            return True
        except Exception as exc:
            self.emit({"type": "error", "code": "workflow_deny_failed", "message": str(exc)})
            return False

    def workflow_stop(self, run_id: str, *, reason: str = "") -> bool:
        run_id = str(run_id or "")
        if not run_id:
            self.emit({"type": "error", "code": "workflow_bad_run_id", "message": "workflow runId is required"})
            return False
        try:
            with backend_output_redirect():
                run = self.workflow_store.load_run(run_id)
                if run.status in {"draft", "awaiting_approval", "interrupted"}:
                    run = self.workflow_controller.cancel(run.run_id, reason=reason or "stopped from Ink bridge")
                elif run.status == "running":
                    run = self.workflow_controller.stop(run.run_id, reason=reason or "stopped from Ink bridge")
                else:
                    self.emit({"type": "workflow_run", "run": self._workflow_run_payload(run)})
                    return True
            self._emit_agent_read_model()
            self.emit({"type": "workflow_run", "run": self._workflow_run_payload(run)})
            self._emit_workflow_events(run.run_id)
            return True
        except Exception as exc:
            self.emit({"type": "error", "code": "workflow_stop_failed", "message": str(exc)})
            return False

    def wait_for_workflow_idle(self, run_id: str, timeout: float | None = None) -> None:
        thread = self._workflow_threads.get(str(run_id or ""))
        if thread is not None:
            thread.join(timeout=timeout)

    def _run_workflow_runtime(self, run_id: str, args: Any, timeout_seconds: float | None, resume_from_run_id: str | None = None) -> None:
        watch_stop = threading.Event()
        watcher = None

        def _stop_watcher() -> None:
            """Stop the progress watcher and wait for its last emit to land.

            ``watch_stop.set()`` only *asks* the watcher to stop; a tick already
            past ``stop.wait()`` still reads the progress file and emits. If that
            read happened just before the run wrote its final snapshot, the stale
            emit can land *after* the terminal ``workflow_progress`` and leave the
            Ink status bar stuck at "3/6 agents done" for a finished 6/6 run.
            Joining here makes the watcher's last emit strictly precede the
            terminal emits below.
            """
            watch_stop.set()
            if watcher is not None:
                try:
                    watcher.join(timeout=2.0)
                except RuntimeError:
                    pass

        try:
            # CC alignment (m1569 @65948 call() returns async_launched; m2000 renders
            # "Running in background · /workflows to monitor and save"): a dynamic
            # workflow is a *background task*. It must never flip the global session
            # status, or the Ink composer locks ("Running · Enter keeps draft") for the
            # whole run. "Running in background" is rendered from the
            # workflow_run / workflow_progress events (the workflow status bar).
            #
            # Progress is durable on disk, so poll it while the runtime blocks.
            # Without this the Ink panel sits at "0/0 agents done" for the whole
            # run and only updates once the runtime returns.
            self.workflow_progress(run_id)
            watcher = threading.Thread(
                target=self._watch_workflow_progress,
                args=(run_id, watch_stop),
                daemon=True,
                name=f"ga-ink-workflow-progress-{run_id}",
            )
            watcher.start()
            with backend_output_redirect():
                run = self.workflow_store.load_run(run_id)
                # The controller already assigned this run its own workspace
                # (``workflow-runs/<runId>/``). Overwriting it here would point
                # every run back at the shared root, which is exactly the
                # cross-run overwrite the per-run directory prevents. Only fill
                # in a default when the run has no workspace of its own.
                if not (isinstance(run.metadata, dict) and run.metadata.get("workspacePath")):
                    if resume_from_run_id is None and self._assign_run_workspace(run):
                        self.workflow_store.save_run(run)
                    else:
                        run.metadata.update(workspace_metadata(self.workspace_root))
                        self.workflow_store.save_run(run)
                runtime = self._make_workflow_runtime(timeout_seconds=timeout_seconds)
                runtime.run(run, args=args, resume_from_run_id=resume_from_run_id)
                current = self.workflow_store.load_run(run_id)
            _stop_watcher()
            self._emit_workflow_events(run_id)
            self.emit({"type": "workflow_run", "run": self._workflow_run_payload(current)})
            self.workflow_progress(run_id)
            self.emit({"type": "workflow_final", "runId": run_id, "result": self._workflow_final_payload(current)})
            self._queue_workflow_handoff(current)
        except Exception as exc:
            _stop_watcher()
            try:
                current = self.workflow_store.load_run(run_id)
                if current.status not in {"succeeded", "degraded", "failed", "killed", "interrupted"}:
                    from genericagent.workflow.models import WorkflowEvent

                    reason = redact_sensitive_text(str(exc))
                    current.status = "failed"
                    current.error = reason
                    self.workflow_store.write_final_result(
                        current,
                        {"runId": run_id, "status": "failed", "error": reason},
                    )
                    self.workflow_store.save_run(current)
                    existing_events = self.workflow_store.replay_events(run_id)
                    self.workflow_store.append_event(
                        current,
                        WorkflowEvent(
                            run_id=current.run_id,
                            session_id=current.session_id,
                            event_type="workflow_failed",
                            sequence=0,
                            payload={"error": reason},
                        ),
                    )
                    current = self.workflow_store.load_run(run_id)
                self._emit_workflow_events(run_id)
                self.emit({"type": "workflow_run", "run": self._workflow_run_payload(current)})
                self.workflow_progress(run_id)
                self.emit({"type": "workflow_final", "runId": run_id, "result": self._workflow_final_payload(current)})
                self._queue_workflow_handoff(current)
            except Exception:
                pass
            self.emit({"type": "error", "code": "workflow_run_failed", "message": str(exc)})
        finally:
            _stop_watcher()

    def _watch_workflow_progress(self, run_id: str, stop: threading.Event, *, interval: float = 0.25) -> None:
        """Publish workflow-progress snapshots until the run settles.

        Cheap by design. The durable snapshot is a file, so a tick that finds it
        unchanged costs a single ``stat``. The previous implementation called
        ``workflow_store.load_run`` (a ~12 ms ``state.json`` read) plus a full
        ``json.dumps`` on *every* tick; that fixed cost, four times a second,
        is what made the Ink input box lag while a workflow ran.
        """
        try:
            with backend_output_redirect():
                artifact_dir = os.path.abspath(os.fspath(self.workflow_store.load_run(run_id).artifact_dir or ""))
        except Exception:
            return
        if not artifact_dir:
            return
        progress_path = os.path.join(artifact_dir, "workflow-progress.json")
        last_progress_signature: tuple[int, int] | None = None
        last_live_fingerprint: str | None = None
        while not stop.wait(interval):
            progress: dict[str, Any] | None = None
            signature = _file_signature(progress_path)
            if signature is not None and signature != last_progress_signature:
                last_progress_signature = signature
                progress = _read_json_object(progress_path)
            live = self._read_live_telemetry(artifact_dir)
            live_fingerprint = json.dumps(live, ensure_ascii=True, sort_keys=True, separators=(",", ":")) if live else None
            if live_fingerprint == last_live_fingerprint:
                live = None
            if progress is None and live is None:
                continue
            # The generic agent read model (list_records + events_since over the
            # whole store) is deliberately NOT published here: it cost ~1.3 s and
            # ~1.6 MB per call and the workflow panel does not use it. The panel is
            # fed by workflow_progress + workflow_live below, exactly like CC's
            # detail view (which reads agent transcripts on demand, m1460).
            if live is not None:
                last_live_fingerprint = live_fingerprint
                try:
                    self.emit({"type": "workflow_live", "runId": run_id, "jobs": live})
                except Exception:
                    continue
            if progress is None:
                continue
            try:
                self.emit({"type": "workflow_progress", "progress": progress})
            except Exception:
                continue
            if str(progress.get("status") or "") in {
                "succeeded",
                "degraded",
                "failed",
                "cancelled",
                "killed",
                "interrupted",
            }:
                return

    def _read_live_telemetry(self, artifact_dir: str) -> list[dict[str, Any]]:
        """Return the per-job live telemetry written by running children.

        Live telemetry is a UI-only observation channel: it never feeds a
        durable contract check, and ``workflow-progress.json`` stays the single
        durable snapshot. It exists because a child only writes its transcript
        when the job *ends*, so a multi-minute child used to show a frozen row.
        """
        jobs: list[dict[str, Any]] = []
        try:
            paths = sorted(glob.glob(os.path.join(artifact_dir, _LIVE_TELEMETRY_RELATIVE)))
        except Exception:
            return jobs
        for path in paths:
            payload = _read_json_object(path)
            if not isinstance(payload, dict):
                continue
            job_id = payload.get("jobId") or os.path.basename(os.path.dirname(path))
            entry = dict(payload)
            entry["jobId"] = str(job_id)
            jobs.append(entry)
        return jobs

    def _effective_workflow_timeout(self, timeout_seconds: float | None) -> float:
        """Resolve product timeout: explicit value wins, else 900s (15 min)."""
        if timeout_seconds is None:
            return float(DEFAULT_WORKFLOW_TIMEOUT_SECONDS)
        value = float(timeout_seconds)
        if value <= 0:
            return float(DEFAULT_WORKFLOW_TIMEOUT_SECONDS)
        return value

    def _make_workflow_runtime(self, *, timeout_seconds: float | None):
        # Always pass an explicit timeout so product path never falls through to
        # WorkflowRuntime's 10s unit-test default.
        kwargs = {
            "store": self.workflow_store,
            "timeout_seconds": self._effective_workflow_timeout(timeout_seconds),
        }
        if self.workflow_runtime_factory is not None:
            return self.workflow_runtime_factory(**kwargs)
        # Production runs use the bridge launch directory as the user
        # workspace. The injected factory branch intentionally stays minimal
        # for unit-test doubles that predate this keyword.
        kwargs["workspace_root"] = str(self.workspace_root)
        with backend_output_redirect():
            from genericagent.workflow.child_agent import NativeGPTChildAgentRunner
            from genericagent.workflow.llm import binding_from_agent
            from genericagent.workflow.runtime import WorkflowRuntime

            agent = self.agent

            def _binding_provider():
                return binding_from_agent(agent)

            runner = NativeGPTChildAgentRunner(
                binding_provider=_binding_provider,
                enable_tools=True,
            )
            # Workflow children share the leading agent's loaded hook registry
            # but receive a child-scoped tool context from _build_handler.
            runner.hook_agent = agent
            kwargs["runner"] = runner
            kwargs["llm_binding_provider"] = _binding_provider
            return WorkflowRuntime(**kwargs)

    def _emit_workflow_events(self, run_id: str) -> None:
        seen = self._workflow_emitted_sequences.setdefault(run_id, set())
        with backend_output_redirect():
            events = self.workflow_store.replay_events(run_id)
        for event in events:
            if event.sequence in seen:
                continue
            seen.add(event.sequence)
            self.emit({"type": "workflow_event", "event": event.to_dict()})

    def _initial_agent_cursors(self) -> dict[str, int]:
        """Start live control streams at their current durable tails.

        The process event bus is shared by all GA sessions. Starting a new Ink
        bridge at cursor 0 replays the entire historical bus in one frame;
        the UI's bounded event list then evicts the freshly spawned child's
        terminal event. Seed only the process cursor here. Workflow cursors
        remain per-run and are initialized by their adapter when a run exists.
        """
        cursors: dict[str, int] = {}
        for adapter in getattr(self.agent_control, "adapters", ()) or ():
            engine = getattr(adapter, "engine", None)
            if engine == "process":
                manager = getattr(adapter, "manager", None)
                bus = getattr(manager, "event_bus", None)
                last_event_seq = getattr(bus, "last_event_seq", None)
                if callable(last_event_seq):
                    try:
                        cursors[str(getattr(adapter, "source_cursor", "process"))] = int(last_event_seq() or 0)
                    except Exception:
                        pass
            elif engine == "workflow":
                # Workflow sources are per-run. Seed existing runs to their
                # current tails so a fresh Ink session does not replay every
                # historical workflow event into the bounded UI event list.
                store = getattr(adapter, "store", None)
                try:
                    from genericagent.core.models import make_workflow_source_cursor
                    for run in store.list_runs() if store is not None else ():
                        rows = store.replay_events(run.run_id)
                        cursors[make_workflow_source_cursor(run.run_id)] = max(
                            (int(row.sequence or 0) for row in rows),
                            default=0,
                        )
                except Exception:
                    pass
        return cursors

    def _emit_agent_read_model(self) -> None:
        self._emit_agent_events()
        self._emit_agent_snapshot()

    def _emit_agent_events(self) -> None:
        try:
            batch = self.agent_control.events_since(self.agent_cursors)
        except Exception as exc:
            message = redact_sensitive_text(str(exc))
            self._agent_errors["control"] = message
            return
        for key, value in batch.next_cursors.items():
            self.agent_cursors[str(key)] = max(self.agent_cursors.get(str(key), 0), int(value))
        for key, value in batch.errors.items():
            self._agent_errors[str(key)] = redact_sensitive_text(str(value))
        for event in batch.events:
            event_id = event.event_id
            if event_id and event_id in self._agent_event_ids:
                continue
            if event_id:
                self._agent_event_ids.add(event_id)
            self.emit({"type": "agent_event", "event": event.to_dict()})

    def _emit_agent_snapshot(self) -> None:
        try:
            records = self.agent_control.list_records(include_terminal=True)
            errors = dict(self._agent_errors)
            errors.update({str(key): redact_sensitive_text(str(value)) for key, value in (getattr(self.agent_control, "last_errors", {}) or {}).items()})
            snapshot = {
                "records": [record.to_dict() for record in records],
                "cursors": dict(self.agent_cursors),
                "errors": errors,
            }
            fingerprint = json.dumps(snapshot, ensure_ascii=True, sort_keys=True, separators=(",", ":"))
            if fingerprint == self._agent_snapshot_fingerprint:
                return
            self._agent_snapshot_fingerprint = fingerprint
            self.emit(
                {
                    "type": "agent_snapshot",
                    "snapshot": snapshot,
                }
            )
        except Exception as exc:
            self._agent_errors["control"] = redact_sensitive_text(str(exc))

    def _resolve_workflow_run_info(self, run_id: str) -> dict[str, Any] | None:
        """Authoritative run status for wait_agent(condition="workflow_terminal").

        The bridge owns the workflow store, so it answers with the same durable
        record /workflows renders. An unknown run id returns None and the wait tool
        fails fast, instead of synthesising a never-terminal subagent state.
        """
        run_id = str(run_id or '').strip()
        if not run_id:
            return None
        try:
            run = self.workflow_store.load_run(run_id)
        except Exception:
            return None
        status = getattr(run, 'status', None)
        thread = getattr(self, '_workflow_threads', {}).get(run_id)
        if thread is not None and not thread.is_alive() and not workflow_run_is_terminal(status):
            # The launcher thread died without writing a terminal status (process kill,
            # unhandled crash outside the runtime's try/except). Report 'stale' so the
            # wait returns instead of blocking the main agent forever.
            status = 'stale'
        artifact_dir = getattr(run, 'artifact_dir', None)
        artifact_path = os.fspath(artifact_dir) if artifact_dir else None
        result_ref = None
        updated_at = None
        if artifact_path:
            candidate = Path(artifact_path) / 'final-result.json'
            if candidate.is_file():
                result_ref = str(candidate)
            state_path = Path(artifact_path) / 'state.json'
            try:
                updated_at = time.strftime('%Y-%m-%dT%H:%M:%S', time.localtime(state_path.stat().st_mtime))
            except OSError:
                updated_at = None
        return {
            'status': status,
            'error': getattr(run, 'error', None),
            'artifactDir': artifact_path,
            'updatedAt': updated_at,
            'resultRef': result_ref,
        }

    def _workflow_run_payload(self, run) -> dict[str, Any]:
        data = run.to_dict()
        data.pop("script", None)
        # CC parity (m0869 Mt()/Co=400): the event channel ships a bounded
        # prompt preview, not the full text. The full prompt is preserved in
        # the durable state.json; only this UI payload is truncated.
        jobs = data.get("jobs")
        if isinstance(jobs, list):
            for job in jobs:
                if isinstance(job, dict) and isinstance(job.get("prompt"), str):
                    # Canonical CC-style preview (workflow_store.preview_text):
                    # 400-char head + ellipsis; the tail is dropped from the
                    # event channel, the durable record keeps the full text.
                    job["prompt"] = preview_text(job["prompt"])
        recovery = self._workflow_recovery_hint(run)
        if recovery:
            data["recovery"] = recovery
        return data

    # CC parity (m1906_0xe19cc02.js): the /workflows history list reads a
    # small per-run snapshot (runId/status/name/agentCount/totals), never the
    # full run record. The list row + status bar only need status, the display
    # name, a few metadata flags, and a per-job {label,status,tokenUsage} -
    # so ship exactly those instead of every job/prompt/artifact field.
    _WORKFLOW_SUMMARY_METADATA_KEYS = (
        "workflowName",
        "workflowTaskType",
        "integrationStatus",
        "finalAuditStatus",
        "launchedBy",
    )
    _WORKFLOW_SUMMARY_JOB_METADATA_KEYS = ("label", "tokenUsage")

    def _workflow_run_summary(self, run) -> dict[str, Any]:
        """Lightweight per-run payload for the ``/workflows`` list.

        Keeps only what ``workflowListRunRow`` and ``workflowStatusBar`` read:
        ``runId``/``sessionId``/``status`` + a few display metadata keys + a
        minimal per-job record. Drops script/jobs.prompt/artifactDir/resultRef/
        error and every other job metadata key, which is what made the list
        payload ~440 KB for one session.
        """
        metadata = run.metadata if isinstance(run.metadata, dict) else {}
        summary_metadata = {
            key: metadata[key]
            for key in self._WORKFLOW_SUMMARY_METADATA_KEYS
            if key in metadata
        }
        jobs: list[dict[str, Any]] = []
        for job in run.jobs or []:
            job_metadata = job.metadata if isinstance(job.metadata, dict) else {}
            entry: dict[str, Any] = {"jobId": job.job_id, "status": job.status}
            slim_metadata = {
                key: job_metadata[key]
                for key in self._WORKFLOW_SUMMARY_JOB_METADATA_KEYS
                if key in job_metadata
            }
            if slim_metadata:
                entry["metadata"] = slim_metadata
            jobs.append(entry)
        return {
            "runId": run.run_id,
            "sessionId": run.session_id,
            "status": run.status,
            "metadata": summary_metadata,
            "jobs": jobs,
        }

    # CC evidence: m0567_0xcdd6311.js CTn() injects
    # "Resume the paused workflow by calling: Workflow({scriptPath, resumeFromRunId})"
    # and the <recovery> block of a failed/killed task-notification says
    # "To resume after editing the script, call: Workflow({...})" plus
    # "Agent transcripts: <dir>". GA has no Workflow() tool, so the same
    # guidance points at the GA resume entry point instead.
    RECOVERABLE_WORKFLOW_STATUSES = frozenset({"failed", "killed", "cancelled", "interrupted"})

    def _workflow_recovery_hint(self, run) -> dict[str, Any] | None:
        status = str(getattr(run, "status", "") or "")
        if status not in self.RECOVERABLE_WORKFLOW_STATUSES:
            return None
        run_id = str(getattr(run, "run_id", "") or "")
        hint: dict[str, Any] = {
            "command": f"/workflow resume {run_id}",
            "message": (
                f"To resume after editing the script, run: /workflow resume {run_id} "
                "— completed agents return cached results."
            ),
        }
        artifact_dir = getattr(run, "artifact_dir", None)
        if artifact_dir:
            hint["agentTranscriptsDir"] = os.path.join(os.fspath(artifact_dir), "agents")
        return hint

    def _workflow_artifact_payload(self, run, artifact_ref: Any) -> dict[str, Any] | None:
        if not run.artifact_dir or not artifact_ref:
            return None
        artifact_dir = os.path.abspath(os.fspath(run.artifact_dir))
        ref = os.fspath(artifact_ref)
        if os.path.isabs(ref):
            return None
        path = os.path.abspath(os.path.join(artifact_dir, ref))
        try:
            if os.path.commonpath([artifact_dir, path]) != artifact_dir:
                return None
        except ValueError:
            return None
        return _read_json_object(path)

    def _run_workspace_root(self, run) -> str:
        """Return the workspace this run's artifacts are rooted at.

        Every run owns ``<base>/workflow-runs/<runId>/`` so concurrent runs
        cannot overwrite each other. Handoff refs must therefore be resolved
        against the run's own workspace, not the shared GA workspace root; the
        base path stays in ``workspaceBasePath`` for human/again-relative
        reporting.
        """
        metadata = run.metadata if isinstance(run.metadata, dict) else {}
        candidate = metadata.get("workspacePath")
        if isinstance(candidate, str) and candidate.strip():
            return os.path.realpath(os.fspath(candidate.strip()))
        return os.path.realpath(os.fspath(self.workspace_root))

    def _run_artifact_base_root(self, run) -> str:
        """Return the root that run-internal refs (result/transcript) resolve under.

        The run's internal artifact directory lives under the GA workspace base
        (``temp/sessions/<session>/workflows/<run>/``), not under the run's own
        ``workflow-runs/<runId>/`` output directory. Those are two different
        roots, and conflating them was what made the handoff unreadable.
        """
        metadata = run.metadata if isinstance(run.metadata, dict) else {}
        candidate = metadata.get("workspaceBasePath")
        if isinstance(candidate, str) and candidate.strip():
            return os.path.realpath(os.fspath(candidate.strip()))
        return os.path.realpath(os.fspath(self.workspace_root))

    def _assign_run_workspace(self, run) -> bool:
        """Give an unassigned run its own workspace directory. Returns success.

        Every direct-JS entry point (inline script, scriptPath, named workflow,
        resume, and nested ``workflow()``) routes through here. Assigning the
        per-run workspace in one place keeps one rule for every entry point: a
        run's deliverables never share a directory with another run's.
        """
        from genericagent.workflow.workspace import create_run_workspace, workspace_metadata

        try:
            directory = create_run_workspace(self.workspace_root, run.run_id)
        except (OSError, RuntimeError, ValueError):
            return False
        metadata = dict(run.metadata) if isinstance(run.metadata, dict) else {}
        metadata.update(workspace_metadata(directory))
        metadata["workspaceBasePath"] = str(resolve_workspace_root(self.workspace_root))
        run.metadata = metadata
        return True

    def _workflow_workspace_ref(self, run, artifact_ref: Any) -> str | None:
        if not run.artifact_dir or not artifact_ref:
            return None
        ref = os.fspath(artifact_ref)
        if os.path.isabs(ref):
            return None
        artifact_dir = os.path.realpath(os.fspath(run.artifact_dir))
        candidate = os.path.realpath(os.path.join(artifact_dir, ref))
        workspace_root = self._run_artifact_base_root(run)
        try:
            if os.path.commonpath([artifact_dir, candidate]) != artifact_dir:
                return None
            if os.path.commonpath([workspace_root, candidate]) != workspace_root:
                return None
        except ValueError:
            return None
        if not os.path.isfile(candidate):
            return None
        return os.path.relpath(candidate, workspace_root).replace(os.sep, '/')

    def _workflow_job_readable_result_ref(self, run, job) -> str | None:
        """Return the host's workspace-relative copy of a job's durable result.

        ``resultRef`` is relative to the run's internal artifact directory. A
        reader that only knows the run workspace joins it onto the wrong root and
        concludes the result is missing, so the handoff publishes the copy the
        host materialised under ``workflow-handoffs/`` instead.
        """
        ref = f"workflow-handoffs/result-{job.job_id}.json"
        workspace = self._run_workspace_root(run)
        if os.path.isfile(os.path.join(workspace, *ref.split('/'))):
            return ref
        return None

    def _workflow_job_readable_result_path(self, run, job) -> str | None:
        ref = self._workflow_job_readable_result_ref(run, job)
        if not ref:
            return None
        return os.path.join(self._run_workspace_root(run), *ref.split('/'))

    def _workflow_job_artifact_refs(self, run, job) -> list[str]:
        """Return workspace-relative files a job actually produced.

        Reads the scheduler's observed-artifact record (ground truth from the
        child's filesystem changes, whatever tool made them) and only keeps
        paths that exist inside the run's workspace, so the value is safe to
        hand to the next LLM request.
        """
        candidates = self._workflow_job_observed_artifacts(run, job)
        if not candidates:
            return []
        workspace_root = self._run_workspace_root(run)
        resolved: list[str] = []
        for candidate in candidates:
            ref = candidate.replace('\\', '/').strip()
            if not ref or os.path.isabs(ref) or ref.startswith('../') or '/../' in ref:
                continue
            target = os.path.realpath(os.path.join(workspace_root, ref))
            try:
                if os.path.commonpath([workspace_root, target]) != workspace_root:
                    continue
            except ValueError:
                continue
            if not os.path.isfile(target):
                continue
            relative = os.path.relpath(target, workspace_root).replace(os.sep, '/')
            if relative not in resolved:
                resolved.append(relative)
        return resolved[:32]

    def _workflow_job_artifact_owners(self, run, refs: list[str]) -> dict[str, list[str]]:
        """Map each artifact ref to the job(s) that wrote it, read run-wide.

        Ownership is recorded at diff time on the job; rebuilding it here only
        unions what already exists, so it stays correct for any writer tool and
        is not a second source of truth. The persisted run-level index carries
        the same information for jobs that ran in a child process and therefore
        kept no local ``handoff`` dict.
        """
        owners = artifact_ownership_index(run.jobs or [])
        if not owners:
            recorded = (run.metadata or {}).get("artifactOwnership")
            if isinstance(recorded, dict):
                owners = {
                    str(ref): [str(writer) for writer in writers]
                    for ref, writers in recorded.items()
                    if isinstance(writers, (list, tuple))
                }
        return {ref: owners[ref] for ref in refs if ref in owners}


    def _workflow_job_observed_artifacts(self, run, job) -> list[str]:
        """Observed artifact paths from the job or its persisted progress record.

        The live ``WorkflowJob`` carries them, but reloaded/older runs only have
        them in ``workflow-progress.json``, so fall back to that snapshot.
        """
        metadata = job.metadata if isinstance(job.metadata, dict) else {}
        observed = observed_artifact_paths(metadata.get('observedArtifacts'))
        if observed:
            return observed
        progress = self._workflow_artifact_payload(run, 'workflow-progress.json')
        if not isinstance(progress, dict):
            return []
        job_id = getattr(job, 'job_id', None)
        for entry in progress.get('workflowProgress') or []:
            if not isinstance(entry, dict):
                continue
            if entry.get('jobId') != job_id and entry.get('agentId') != job_id:
                continue
            return observed_artifact_paths(entry.get('observedArtifacts'))
        return []

    @staticmethod
    def _handoff_bounded_value(value: Any, max_bytes: int) -> Any:
        sanitized = sanitize(value)
        encoded = json.dumps(sanitized, ensure_ascii=False, separators=(',', ':')).encode('utf-8')
        if len(encoded) <= max_bytes:
            return sanitized
        if isinstance(sanitized, dict):
            compact = {}
            for key in ('runId', 'status', 'error', 'workspacePath', 'workspaceBasePath', 'artifactRefs', 'artifactOwners', 'summary', 'answer', 'conclusion', 'result', 'text'):
                if key in sanitized:
                    compact[key] = GenericAgentBridge._handoff_bounded_value(sanitized[key], max(256, max_bytes // 2))
            compact['truncated'] = True
            while len(json.dumps(compact, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) > max_bytes:
                removable = next((key for key in reversed(list(compact)) if key not in {'runId', 'status', 'workspacePath', 'workspaceBasePath', 'artifactRefs', 'artifactOwners', 'truncated'}), None)
                if removable is None:
                    break
                compact.pop(removable)
            return compact
        if isinstance(sanitized, list):
            compact = []
            for item in sanitized:
                candidate = compact + [GenericAgentBridge._handoff_bounded_value(item, max(256, max_bytes // 2))]
                if len(json.dumps(candidate, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) > max_bytes - 32:
                    break
                compact = candidate
            return {'items': compact, 'truncated': True}
        return {'truncated': True, 'preview': encoded[:max(0, max_bytes - 96)].decode('utf-8', errors='ignore')}

    def _workflow_handoff_payload(self, run) -> str:
        metadata = run.metadata if isinstance(run.metadata, dict) else {}
        # The JSON-planner draft no longer exists: the model-authored script is
        # the plan. The originating request is carried on run metadata by the
        # ``workflow`` tool entry, with the workflow name as a last resort.
        task_text = str(metadata.get('originalTask') or metadata.get('workflowName') or '')
        if len(task_text.encode('utf-8')) > 8192:
            task_text = task_text.encode('utf-8')[:8000].decode('utf-8', errors='ignore') + ' …[task truncated]'

        intermediate = []
        for job in run.jobs or []:
            result_ref = job.result_ref or f'agents/{job.job_id}/result.json'
            artifact = self._workflow_artifact_payload(run, result_ref)
            transcript_ref = (job.metadata or {}).get('transcriptRef') if isinstance(job.metadata, dict) else None
            transcript_ref = transcript_ref or f'agents/{job.job_id}/transcript.jsonl'
            item = {
                'jobId': job.job_id,
                'status': job.status,
                'resultRef': self._workflow_job_readable_result_ref(run, job) or self._workflow_workspace_ref(run, result_ref),
                'transcriptRef': self._workflow_workspace_ref(run, transcript_ref),
            }
            readable_path = self._workflow_job_readable_result_path(run, job)
            if readable_path:
                item['resultPath'] = readable_path
            # Files the child actually wrote, as workspace-relative paths. The
            # handoff otherwise carries only logical labels ("synthesis"), so the
            # GA agent has to guess a location and can read the wrong directory.
            artifacts = self._workflow_job_artifact_refs(run, job)
            if artifacts:
                item['artifactRefs'] = artifacts
                # Which job wrote each ref. Without it a downstream reader sees
                # ``report.md`` but cannot tell whether it is research output or
                # synthesis output -- or that both jobs wrote the same path.
                owners = self._workflow_job_artifact_owners(run, artifacts)
                if owners:
                    item['artifactOwners'] = owners
            if artifact and job.status in {'succeeded', 'cached'}:
                item['result'] = self._handoff_bounded_value(artifact.get('payload', artifact), 8192)
            intermediate.append(item)

        run_metadata = run.metadata if isinstance(run.metadata, dict) else {}
        run_workspace = self._run_workspace_root(run)
        base_workspace = run_metadata.get("workspaceBasePath")
        collisions = run_metadata.get("artifactCollisions")
        failed_jobs = [
            {
                'jobId': job.job_id,
                'label': (job.metadata or {}).get('label') if isinstance(job.metadata, dict) else None,
                'phase': job.phase,
                'status': job.status,
                'error': redact_sensitive_text(str(job.error)) if job.error else None,
            }
            for job in (run.jobs or [])
            if job.status not in {'succeeded', 'cached', 'skipped'}
        ]
        handoff = {
            'runId': run.run_id,
            'status': run.status,
            'error': redact_sensitive_text(str(run.error)) if run.error else None,
            'originalTask': task_text,
            'finalResult': self._handoff_bounded_value(self._workflow_final_payload(run), 28 * 1024),
            'finalResultRef': self._workflow_workspace_ref(run, run.result_ref),
            'intermediateResults': intermediate,
            # Concrete absolute root for this run's deliverables. Every
            # ``artifactRefs``/``finalResultRef`` entry is relative to it, so the
            # GA agent never has to infer the location from the run's internal
            # artifact directory.
            'workspacePath': run_workspace,
            'workspaceBasePath': str(base_workspace) if base_workspace else run_workspace,
        }
        if failed_jobs:
            # CC evidence (dynamic-workflow-internals.md 8.3/8.4): the completion
            # notification carries the agent <failures> list, so a degraded run is
            # never reported as a clean success.
            handoff['failedJobs'] = failed_jobs
        journal_path = None
        if run.artifact_dir:
            candidate_journal = Path(os.fspath(run.artifact_dir)) / 'journal.jsonl'
            if candidate_journal.is_file():
                journal_path = str(candidate_journal)
        if journal_path:
            # CC 8.3 diagnostics: point the reader at the per-agent journal BEFORE
            # it diagnoses, so an empty finalResult is not mistaken for 'no work'.
            handoff['journalRef'] = journal_path
        if isinstance(collisions, dict) and collisions:
            # Two jobs wrote the same path, so the later write won. The reader
            # must know the file may not represent the earlier job's output.
            handoff['artifactCollisions'] = sanitize(copy.deepcopy(collisions))
        side_effects = run_metadata.get('verificationSideEffects')
        if isinstance(side_effects, dict) and side_effects:
            # A verification job ran the artifact it was checking, so the run
            # rewrote it. Reported separately from a producer collision: this is
            # the same deliverable re-generated by the check, not a second one.
            handoff['verificationSideEffects'] = sanitize(copy.deepcopy(side_effects))
        # Run-level ownership, always derived here as well: a stage that ran in a
        # child process keeps no local handoff dict, and runs recorded before the
        # index existed have no ``artifactOwnership`` in metadata either. Both
        # sources are the same diff-time writer record, so a union is safe.
        ownership = build_artifact_ownership_index(run)
        if not ownership and isinstance(run_metadata.get("artifactOwnership"), dict):
            ownership = run_metadata["artifactOwnership"]
        if ownership:
            handoff['artifactOwnership'] = sanitize(copy.deepcopy(ownership))
        while len(json.dumps(handoff, ensure_ascii=False, separators=(',', ':')).encode('utf-8')) > _WORKFLOW_HANDOFF_MAX_BYTES - 512:
            candidate = next((item for item in reversed(intermediate) if 'result' in item), None)
            if candidate is not None:
                candidate.pop('result', None)
                continue
            if intermediate:
                intermediate.pop()
                continue
            handoff['finalResult'] = self._handoff_bounded_value(handoff['finalResult'], 16 * 1024)
            handoff['originalTask'] = str(handoff['originalTask'])[:1024] + ' …[task truncated]'
            break
        serialized = json.dumps(handoff, ensure_ascii=False, indent=2)
        return (
            "The workflow run has finished. Answer the user's original request using the final result and relevant intermediate results below. "
            "Do not claim success if the workflow status is failed/cancelled. "
            "Path bases are explicit and they differ: `finalResultRef` and every `artifactRefs` entry are relative "
            "to `workspacePath` above (this run's own directory under `workspaceBasePath`); "
            "`intermediateResults[].resultRef` and `intermediateResults[].transcriptRef` are run-internal and "
            "relative to `workspaceBasePath`, never to `workspacePath`. Read them only if needed. "
            "`artifactOwners` says which job wrote each ref, so prefer reading a ref when you need that stage's own output; "
            "`artifactOwnership` is the same run-wide `path -> writer(s)` index and also covers stages that ran in a child process. "
            "If `artifactCollisions` lists a path, more than one job wrote it and the last write won: "
            "treat that file as possibly not reflecting the earlier writer, and do not assume every job's output survived. "
            "`verificationSideEffects` lists paths a verification job touched while running the artifact under "
            "test: that is the same deliverable re-generated by the check, not a second output. "
            "Per-agent results: read `journalRef` (journal.jsonl) — one {\"type\":\"result\"} line per completed "
            "agent with its full return value; if `finalResult` is empty or unexpected, read it BEFORE diagnosing. "
            "`failedJobs` lists the jobs that did not succeed. "
            "Transcript files are references only and are not included here.\n\n"
            f'<workflow_handoff>\n{serialized}\n</workflow_handoff>'
        )

    def _queue_workflow_handoff(self, run) -> bool:
        # Unit/test doubles may not own a live agent loop. In production the
        # bridge starts a persistent agent thread which can consume this task;
        # without that consumer, retain the legacy terminal-idle behavior.
        agent_thread = getattr(self, "_agent_thread", None)
        if agent_thread is not None and hasattr(agent_thread, "is_alive") and not agent_thread.is_alive():
            return False
        run_id = str(run.run_id)
        with self._workflow_handoff_lock:
            if run_id in self._workflow_handed_off:
                return True
            self._workflow_handed_off.add(run_id)
        try:
            prompt = self._workflow_handoff_payload(run)
            self._task_seq += 1
            display_queue = self.agent.put_task(prompt, source='workflow_handoff', images=[])
            # The workflow itself is a background task, but the *handoff* (the main
            # agent answering with the workflow result) is a real agent turn: emit
            # the same running/idle pair a user submit does, so the composer locks
            # only while the agent is actually generating the final answer.
            self.emit({"type": "status", "status": "running", "taskId": self._task_seq})
            self._consume_thread = threading.Thread(
                target=self._consume_display_queue,
                args=(self._task_seq, display_queue),
                daemon=True,
                name=f'ga-ink-workflow-handoff-{self._task_seq}',
            )
            self._consume_thread.start()
            return True
        except Exception as exc:
            with self._workflow_handoff_lock:
                self._workflow_handed_off.discard(run_id)
            self.emit({'type': 'error', 'code': 'workflow_handoff_failed', 'message': redact_sensitive_text(str(exc))})
            return False

    def _workflow_final_payload(self, run) -> dict[str, Any]:
        if not run.artifact_dir or not run.result_ref:
            return self._workflow_final_fallback(run, "missing_ref")
        result_path, artifact_error = self._workflow_result_path(run)
        if artifact_error:
            return self._workflow_final_fallback(run, artifact_error)
        try:
            size = os.path.getsize(result_path)
        except Exception:
            return self._workflow_final_fallback(run, "read_failed")
        if size > _WORKFLOW_FINAL_PAYLOAD_MAX_BYTES:
            return self._workflow_final_fallback(
                run,
                "too_large",
                artifact_truncated=True,
                artifact_size=size,
            )
        try:
            with open(result_path, "r", encoding="utf-8", errors="replace") as fh:
                payload = json.load(fh)
        except json.JSONDecodeError:
            return self._workflow_final_fallback(run, "invalid_json")
        except Exception:
            return self._workflow_final_fallback(run, "read_failed")
        if not isinstance(payload, dict):
            return self._workflow_final_fallback(run, "invalid_payload")
        return sanitize(payload)

    def _workflow_result_path(self, run) -> tuple[str | None, str | None]:
        artifact_dir = os.path.abspath(os.fspath(run.artifact_dir))
        result_ref = os.fspath(run.result_ref)
        if os.path.isabs(result_ref):
            return None, "invalid_result_ref"
        result_path = os.path.abspath(os.path.join(artifact_dir, result_ref))
        try:
            if os.path.commonpath([artifact_dir, result_path]) != artifact_dir:
                return None, "invalid_result_ref"
        except ValueError:
            return None, "invalid_result_ref"
        if not os.path.isfile(result_path):
            return result_path, "missing"
        return result_path, None

    def _workflow_final_fallback(
        self,
        run,
        artifact_error: str,
        *,
        artifact_truncated: bool = False,
        artifact_size: int | None = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "runId": run.run_id,
            "status": run.status,
            "error": redact_sensitive_text(str(run.error)) if run.error is not None else None,
            "resultRef": run.result_ref,
            "artifactError": artifact_error,
        }
        if artifact_truncated:
            payload["artifactTruncated"] = True
        if artifact_size is not None:
            payload["artifactSize"] = artifact_size
        return sanitize(payload)

    def _current_session_id(self) -> str | None:
        value = str(getattr(self.agent, "session_id", "") or "").strip()
        return value or None

    def _list_workflow_runs(self):
        # CC's ``/workflows`` is per session (m1906: "No dynamic workflows in this
        # session."). temp/sessions is shared by every GA session, so an unscoped
        # scan both diverged from CC and loaded every historical run on each call.
        runs = []
        with backend_output_redirect():
            try:
                runs = list(self.workflow_store.list_runs(session_id=self._current_session_id()))
            except Exception:
                runs = []
        runs.sort(key=lambda run: str(run.run_id))
        return runs

    def _auto_compact_if_needed(self, pending_text: str) -> bool:
        if compact_agent_context is None or should_auto_compact_agent is None:
            return True
        # 熔断器：连续失败达上限后，本 session 停用自动压缩，避免摘要模型宕机时
        # 每次请求都空打一发（对齐 Claude Code）。放行请求（返回 True），让用户
        # 仍能继续对话（硬裁剪安全网在 genericagent.llm.core 层仍生效）。
        if self._auto_compact_failures >= MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES:
            return True
        try:
            if not should_auto_compact_agent(self.agent, pending_text=pending_text):
                return True
            with backend_output_redirect():
                result = compact_agent_context(self.agent, instructions="Automatic compact before the next user request.")
            if result.ok:
                self._auto_compact_failures = 0
                self._replace_compact_log()
                self._record_compact_transcript(result.message)
                self._rewind_snapshots.clear()
                text = "Auto " + result.message
                # 与手动 /compact 相同：成功只 emit history_replace，避免 Static 重复行。
                self.emit({"type": "history_replace", "messages": [
                    {"role": "system", "text": text},
                ]})
                return True
            else:
                self._auto_compact_failures += 1
                self.emit({"type": "error", "code": "auto_compact_failed", "message": result.message})
                if self._auto_compact_failures >= MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES:
                    self.emit({"type": "local_command_output",
                               "text": f"[auto-compact disabled after {MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES} consecutive failures this session]"})
                return False
        except Exception as exc:
            self._auto_compact_failures += 1
            self.emit({"type": "error", "code": "auto_compact_failed", "message": str(exc)})
            if self._auto_compact_failures >= MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES:
                self.emit({"type": "local_command_output",
                           "text": f"[auto-compact disabled after {MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES} consecutive failures this session]"})
            return False

    def _replace_compact_log(self) -> None:
        if replace_log_with_compact_history is None:
            return
        try:
            log_path = getattr(self.agent, "log_path", None)
            replace_log_with_compact_history(log_path, copy.deepcopy(self._backend_history()))
        except Exception as exc:
            self.emit({"type": "error", "code": "compact_log_failed", "message": str(exc)})

    def _record_compact_transcript(self, message: str) -> None:
        if session_transcript is None or not getattr(self.agent, "session_path", None):
            return
        try:
            session_transcript.record_compact(
                self.agent.session_path,
                session_id=getattr(self.agent, "session_id", ""),
                message=message,
                backend_history_after=copy.deepcopy(self._backend_history()),
            )
        except Exception as exc:
            self.emit({"type": "error", "code": "compact_transcript_failed", "message": str(exc)})

    def list_resume_sessions(self) -> None:
        if continue_list is None:
            self.emit({"type": "error", "code": "resume_unavailable", "message": "/resume is unavailable"})
            return
        try:
            sessions = continue_list(**self._resume_exclusion_kwargs())
            self.emit(
                {
                    "type": "resume_sessions",
                    "sessions": [
                        {
                            "id": path,
                            "mtime": float(mtime),
                            "preview": str(preview or ""),
                            "rounds": int(rounds),
                        }
                        for path, mtime, preview, rounds in sessions
                    ],
                }
            )
        except Exception as exc:
            self.emit({"type": "error", "code": "resume_list_failed", "message": str(exc)})

    def resume_session_by_index(self, index: int) -> None:
        if continue_list is None:
            self.emit({"type": "error", "code": "resume_unavailable", "message": "/resume is unavailable"})
            return
        try:
            sessions = continue_list(**self._resume_exclusion_kwargs())
            idx = int(index) - 1
            if not (0 <= idx < len(sessions)):
                self.emit({"type": "system", "text": f"索引越界（有效范围 1-{len(sessions)}）"})
                return
            self.resume_session(str(sessions[idx][0]))
        except Exception as exc:
            self.emit({"type": "error", "code": "resume_failed", "message": str(exc)})

    def resume_session(self, path: str) -> None:
        if continue_reset is None or continue_restore is None or continue_extract is None:
            self.emit({"type": "error", "code": "resume_unavailable", "message": "/resume is unavailable"})
            return
        if getattr(self.agent, "is_running", False) or self._is_consuming():
            self.emit({"type": "error", "code": "busy", "message": "agent is running"})
            return
        try:
            continue_reset(self.agent, message=None)
            result, _ = continue_restore(self.agent, path)
            if str(result).startswith(("✅", "✓")):
                self.emit({"type": "history_replace", "messages": self._resume_ui_messages_with_checkpoints(path)})
            self.emit({"type": "system", "text": str(result)})
        except Exception as exc:
            self.emit({"type": "error", "code": "resume_failed", "message": str(exc)})

    def rewind(self, task_id: int) -> None:
        try:
            task_id = int(task_id)
        except Exception:
            self.emit({"type": "error", "code": "bad_rewind_target", "message": str(task_id)})
            return
        if getattr(self.agent, "is_running", False) or self._is_consuming():
            self.emit({"type": "error", "code": "busy", "message": "agent is running"})
            return
        snapshot = self._rewind_snapshots.get(task_id)
        if snapshot is None:
            self.emit({"type": "error", "code": "rewind_missing", "message": f"no checkpoint for task {task_id}"})
            return
        self._restore_agent_state(snapshot)
        self._record_rewind_transcript(snapshot)
        for stale_id in [key for key in self._rewind_snapshots if key > task_id]:
            del self._rewind_snapshots[stale_id]
        self._task_seq = task_id - 1
        self.emit({"type": "rewind_done", "taskId": task_id, "text": str(snapshot.get("text") or "")})

    def _snapshot_agent_state(self) -> dict[str, Any]:
        return {
            "text": "",
            "history": copy.deepcopy(getattr(self.agent, "history", [])),
            "backend_history": copy.deepcopy(self._backend_history()),
            "last_tools": copy.deepcopy(getattr(getattr(self.agent, "llmclient", None), "last_tools", "")),
            "session_turn_id": int(getattr(self.agent, "session_turn_id", 0) or 0),
        }

    def _restore_agent_state(self, snapshot: dict[str, Any]) -> None:
        try:
            self.agent.abort()
        except Exception:
            pass
        if hasattr(self.agent, "history"):
            self.agent.history = copy.deepcopy(snapshot.get("history") or [])
        backend = getattr(getattr(self.agent, "llmclient", None), "backend", None)
        if backend is not None and hasattr(backend, "history"):
            backend.history = copy.deepcopy(snapshot.get("backend_history") or [])
        client = getattr(self.agent, "llmclient", None)
        if client is not None and hasattr(client, "last_tools"):
            client.last_tools = copy.deepcopy(snapshot.get("last_tools") or "")
        if hasattr(self.agent, "handler"):
            self.agent.handler = None
        if hasattr(self.agent, "session_turn_id"):
            self.agent.session_turn_id = int(snapshot.get("session_turn_id", 0) or 0)

    def _record_rewind_transcript(self, snapshot: dict[str, Any]) -> None:
        if session_transcript is None or not getattr(self.agent, "session_path", None):
            return
        keep_turns = int(snapshot.get("session_turn_id", 0) or 0)
        try:
            session_transcript.record_rewind(
                self.agent.session_path,
                session_id=getattr(self.agent, "session_id", ""),
                keep_turns=keep_turns,
                backend_history_after=copy.deepcopy(snapshot.get("backend_history") or []),
            )
        except Exception as exc:
            self.emit({"type": "error", "code": "rewind_transcript_failed", "message": str(exc)})

    def _backend_history(self) -> Any:
        backend = getattr(getattr(self.agent, "llmclient", None), "backend", None)
        return getattr(backend, "history", [])

    def _backend_token_usage(self) -> dict[str, int] | None:
        backend = getattr(getattr(self.agent, "llmclient", None), "backend", None)
        usage = getattr(backend, "last_usage_tokens", None)
        if not isinstance(usage, dict):
            return None
        try:
            result = {
                "inputTokens": int(usage.get("input_tokens") or 0),
                "outputTokens": int(usage.get("output_tokens") or 0),
                "totalTokens": int(usage.get("total_tokens") or 0),
            }
            cached = int(usage.get("cached_tokens") or 0)
            cache_read = int(usage.get("cache_read_tokens") or 0)
            cache_creation = int(usage.get("cache_creation_tokens") or 0)
            if cached or cache_read or cache_creation:
                result.update({
                    "cachedTokens": cached,
                    "cacheReadTokens": cache_read,
                    "cacheCreationTokens": cache_creation,
                })
            return result
        except Exception:
            return None

    def _resume_exclusion_kwargs(self) -> dict[str, Any]:
        return {
            "exclude_pid": os.getpid(),
            "exclude_path": getattr(self.agent, "log_path", None),
            "exclude_session_id": getattr(self.agent, "session_id", None),
        }

    def _resume_ui_messages_with_checkpoints(self, path: str) -> list[dict[str, Any]]:
        if session_transcript is not None and session_transcript.is_transcript_path(path):
            try:
                loaded = session_transcript.load_session(path)
            except Exception:
                loaded = None
            if loaded is not None:
                messages: list[dict[str, Any]] = []
                self._task_seq = 0
                self._rewind_snapshots.clear()
                for turn in loaded.turns:
                    self._task_seq += 1
                    task_id = self._task_seq
                    messages.append({"role": "user", "text": turn.user_text, "taskId": task_id})
                    self._rewind_snapshots[task_id] = {
                        "text": turn.user_text,
                        "history": [],
                        "backend_history": copy.deepcopy(turn.backend_history_before),
                        "last_tools": "",
                        "session_turn_id": task_id - 1,
                    }
                    if turn.assistant_text:
                        messages.append({"role": "assistant", "text": turn.assistant_text, "taskId": task_id})
                return messages
        backend_history = copy.deepcopy(self._backend_history())
        messages: list[dict[str, Any]] = []
        self._task_seq = 0
        self._rewind_snapshots.clear()
        current_task_id: int | None = None
        user_count = 0
        for item in continue_extract(path):
            role = str(item.get("role", "system"))
            text = str(item.get("content", ""))
            msg: dict[str, Any] = {"role": role, "text": text}
            if role == "user":
                user_count += 1
                self._task_seq += 1
                current_task_id = self._task_seq
                msg["taskId"] = current_task_id
                self._rewind_snapshots[current_task_id] = {
                    "text": text,
                    "history": [],
                    "backend_history": copy.deepcopy(backend_history[: max(0, (user_count - 1) * 2)]),
                    "last_tools": "",
                    "session_turn_id": current_task_id - 1,
                }
            elif role == "assistant" and current_task_id is not None:
                msg["taskId"] = current_task_id
            messages.append(msg)
        return messages

    def wait_for_idle(self, timeout: float | None = None) -> None:
        if self._consume_thread is not None:
            self._consume_thread.join(timeout=timeout)

    def _is_consuming(self) -> bool:
        return self._consume_thread is not None and self._consume_thread.is_alive()

    def _consume_display_queue(self, task_id: int, display_queue: queue.Queue) -> None:
        last_usage = None
        def emit_usage_if_changed() -> None:
            nonlocal last_usage
            usage = self._backend_token_usage()
            if usage is None or usage == last_usage:
                return
            last_usage = usage
            self.emit({"type": "token_usage", "taskId": task_id, **usage})
        try:
            while True:
                item = display_queue.get()
                emit_usage_if_changed()
                if "next" in item:
                    self.emit({"type": "assistant_delta", "taskId": task_id, "text": str(item.get("next") or "")})
                if "done" in item:
                    emit_usage_if_changed()
                    self.emit({"type": "assistant_done", "taskId": task_id, "text": str(item.get("done") or "")})
                    self.emit({"type": "status", "status": "idle", "taskId": task_id})
                    return
        except Exception as exc:
            self.emit({"type": "error", "code": "consume_failed", "message": str(exc), "taskId": task_id})
            self.emit({"type": "status", "status": "idle", "taskId": task_id})


def run_jsonl_loop(stdin: TextIO = sys.stdin, stdout: TextIO = sys.stdout) -> int:
    bridge = GenericAgentBridge(emit=make_stdout_emitter(stdout))
    bridge.emit({"type": "ready", "version": 1})
    for line in stdin:
        line = line.strip()
        if not line:
            continue
        try:
            command = json.loads(line)
        except json.JSONDecodeError as exc:
            bridge.emit({"type": "error", "code": "bad_json", "message": str(exc)})
            continue
        cmd_type = command.get("type")
        if cmd_type == "submit":
            images = command.get("images")
            if images is not None and not isinstance(images, list):
                images = []
            bridge.submit(str(command.get("text") or ""), images=images)
        elif cmd_type == "stop":
            bridge.stop()
        elif cmd_type == "new_session":
            bridge.new_session()
        elif cmd_type == "list_resume_sessions":
            bridge.list_resume_sessions()
        elif cmd_type == "resume_session":
            bridge.resume_session(str(command.get("id") or ""))
        elif cmd_type == "resume_session_index":
            bridge.resume_session_by_index(int(command.get("index") or 0))
        elif cmd_type == "rewind":
            bridge.rewind(int(command.get("taskId") or 0))
        elif cmd_type == "mcp_status":
            bridge.mcp_status()
        elif cmd_type == "mcp_watch_start":
            bridge.start_mcp_status_watch()
        elif cmd_type == "mcp_reconnect":
            bridge.mcp_reconnect(str(command.get("server") or ""))
        elif cmd_type == "mcp_enable":
            bridge.mcp_enable(str(command.get("server") or ""))
        elif cmd_type == "mcp_disable":
            bridge.mcp_disable(str(command.get("server") or ""))
        elif cmd_type == "model_status":
            bridge.model_status()
        elif cmd_type == "model_switch":
            selector = str(command.get("selector") or "")
            if command.get("reasoningEffort") is None:
                bridge.model_switch(selector)
            else:
                bridge.model_switch(
                    selector,
                    reasoning_effort=str(command.get("reasoningEffort")).strip().lower(),
                )
        elif cmd_type == "reasoning_effort_switch":
            if getattr(bridge.agent, "is_running", False) or bridge._is_consuming():
                bridge.emit({"type": "error", "code": "busy", "message": "agent is running"})
            else:
                try:
                    with backend_output_redirect():
                        result = bridge.agent.select_reasoning_effort(str(command.get("effort") or ""))
                    bridge.emit({
                        "type": "reasoning_effort_switch_result",
                        "ok": bool(result.get("ok")),
                        "message": str(result.get("message") or result.get("effort") or ""),
                    })
                except Exception as exc:
                    bridge.emit({"type": "error", "code": "reasoning_effort_switch_failed", "message": str(exc)})
            bridge.model_status()
        elif cmd_type == "permission_status":
            bridge.permission_status()
        elif cmd_type == "set_permission_mode":
            bridge.permission_switch(
                str(command.get("mode") or ""),
                persist=bool(command.get("persist")),
            )
        elif cmd_type == "permission_response":
            bridge.permission_response(
                str(command.get("requestId") or command.get("request_id") or ""),
                str(command.get("decision") or ""),
            )
        elif cmd_type == "skill_status":
            bridge.skill_status()
        elif cmd_type == "skill_invoke":
            bridge.skill_invoke(str(command.get("skill") or ""), str(command.get("args") or ""))
        elif cmd_type == "compact":
            bridge.compact(str(command.get("instructions") or ""))
        elif cmd_type == "workflow_draft":
            bridge.workflow_draft(str(command.get("script") or ""))
        elif cmd_type == "workflow_approve":
            raw_timeout = command.get("timeoutSeconds") or command.get("timeout_seconds")
            timeout_seconds = float(raw_timeout) if raw_timeout is not None else None
            bridge.workflow_approve(str(command.get("runId") or command.get("run_id") or ""), args=command.get("args"), timeout_seconds=timeout_seconds)
        elif cmd_type == "workflow_resume":
            raw_timeout = command.get("timeoutSeconds") or command.get("timeout_seconds")
            timeout_seconds = float(raw_timeout) if raw_timeout is not None else None
            bridge.workflow_resume(str(command.get("runId") or command.get("run_id") or ""), args=command.get("args"), timeout_seconds=timeout_seconds)
        elif cmd_type == "workflow_save":
            bridge.workflow_save(
                str(command.get("runId") or command.get("run_id") or ""),
                name=str(command.get("name") or "") or None,
                overwrite=bool(command.get("overwrite")),
            )
        elif cmd_type == "workflow_run_named":
            raw_timeout = command.get("timeoutSeconds") or command.get("timeout_seconds")
            timeout_seconds = float(raw_timeout) if raw_timeout is not None else None
            bridge.workflow_run_named(
                str(command.get("name") or ""),
                args=command.get("args"),
                timeout_seconds=timeout_seconds,
            )
        elif cmd_type == "workflow_named_list":
            bridge.workflow_named_list()
        elif cmd_type == "team_list":
            bridge.team_list()
        elif cmd_type == "hooks_status":
            bridge.hooks_status()
        elif cmd_type == "workflow_list":
            bridge.workflow_list()
        elif cmd_type == "workflow_detail":
            bridge.workflow_detail(str(command.get("runId") or command.get("run_id") or ""))
        elif cmd_type == "workflow_progress":
            bridge.workflow_progress(str(command.get("runId") or command.get("run_id") or ""))
        elif cmd_type == "workflow_deny":
            bridge.workflow_deny(str(command.get("runId") or command.get("run_id") or ""), reason=str(command.get("reason") or ""))
        elif cmd_type == "workflow_stop":
            bridge.workflow_stop(str(command.get("runId") or command.get("run_id") or ""), reason=str(command.get("reason") or ""))
        elif cmd_type == "shutdown":
            bridge.stop()
            try:
                with backend_output_redirect():
                    from genericagent.mcp.runtime import reset_mcp_manager

                    reset_mcp_manager()
            except Exception:
                pass
            return 0
        else:
            bridge.emit({"type": "error", "code": "unknown_command", "message": str(cmd_type)})
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="GenericAgent JSONL bridge for the Ink frontend")
    parser.parse_args(argv)
    return run_jsonl_loop()


if __name__ == "__main__":
    raise SystemExit(main())
