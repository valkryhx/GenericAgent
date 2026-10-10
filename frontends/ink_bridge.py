"""JSONL bridge for the experimental React/Ink frontend.

This module intentionally stays independent from tuiapp_v2.py.  It exposes a
small stdin/stdout protocol so a Node/Ink process can drive GenericAgent without
embedding Python UI code.
"""

from __future__ import annotations

import argparse
import copy
import contextlib
import json
import os
import queue
import sys
import threading
from typing import Any, Callable, TextIO


PROJECT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

from sensitive_redaction import sanitize, redact_sensitive_text
from workflow_store import build_artifact_ownership_index
from workflow_workspace import (
    default_workspace_root,
    observed_artifact_paths,
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


def make_stdout_emitter(stdout: TextIO) -> EmitFn:
    lock = threading.Lock()

    def emit(event: Event) -> None:
        with lock:
            stdout.write(encode_event(sanitize(event)))
            stdout.flush()

    return emit


def default_agent_factory() -> Any:
    with backend_output_redirect():
        from agentmain import GenericAgent

    # Slice D3：启动时清理过期/超量的 GA 自产剪贴板图（不碰用户原图）
    try:
        from image_gc import maybe_gc_ga_images_on_startup

        maybe_gc_ga_images_on_startup(quiet=True)
    except Exception:
        pass

    agent = GenericAgent()
    agent.inc_out = True
    agent.verbose = True
    return agent


try:
    from compact_context import compact_agent_context, replace_log_with_compact_history, should_auto_compact_agent
except Exception:  # pragma: no cover - compact core import failures are reported at call sites
    compact_agent_context = None
    replace_log_with_compact_history = None
    should_auto_compact_agent = None


# 自动压缩熔断器：连续失败这么多次后，本 session 停用自动压缩，避免摘要模型宕机时
# 每次用户请求都空打一次 API。对齐 Claude Code 的 MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES。
MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES = 3


try:
    from continue_cmd import (
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
    import session_transcript
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
        workflow_planner_factory: Callable[[], Any] | None = None,
        agent_control: Any | None = None,
    ) -> None:
        self.agent_factory = agent_factory
        # Artifacts must not land in GA's own source tree. ``os.getcwd()`` was
        # the repository root for a normal ``ga`` launch, so a generated report
        # appeared next to agentmain.py. The canonical root is the project's
        # gitignored temp/ directory.
        self.workspace_root = resolve_workspace_root(workspace_root or default_workspace_root())
        with backend_output_redirect():
            self.agent = self.agent_factory()
            self.agent.inc_out = True
            self.agent.verbose = True
        raw_emit = emit or make_stdout_emitter(sys.stdout)
        self.emit = lambda event: raw_emit(sanitize(event))
        # ask 档阻塞审批：把 emit 注入 agent.permission_runtime，使 dispatch 可发 permission_request
        try:
            runtime = getattr(self.agent, "permission_runtime", None)
            if runtime is not None and hasattr(runtime, "set_emit"):
                runtime.set_emit(self.emit)
        except Exception:
            pass
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
        self._workflow_emitted_sequences: dict[str, set[int]] = {}
        self._mcp_watch_lock = threading.Lock()
        self._mcp_watch_thread: threading.Thread | None = None
        self._workflow_planning_lock = threading.Lock()
        self._workflow_planning = False
        self._workflow_planning_thread: threading.Thread | None = None
        self.workflow_runtime_factory = workflow_runtime_factory
        self.workflow_planner_factory = workflow_planner_factory
        with backend_output_redirect():
            from workflow_controller import WorkflowController
            from workflow_store import WorkflowStore

        self.workflow_store = WorkflowStore(root=workflow_root)
        self.workflow_controller = WorkflowController(store=self.workflow_store)
        if agent_control is None:
            with backend_output_redirect():
                from agent_control import UnifiedAgentControl
                from agent_control_process import ProcessSubagentAdapter
                from agent_control_workflow import WorkflowChildAdapter
                from subagent_manager import SubagentManager

            process_manager = SubagentManager(root_dir=PROJECT_DIR, python_executable=sys.executable)
            agent_control = UnifiedAgentControl(
                [
                    ProcessSubagentAdapter(process_manager),
                    WorkflowChildAdapter(self.workflow_store, self.workflow_controller),
                ]
            )
        self.agent_control = agent_control
        self.agent_cursors: dict[str, int] = self._initial_agent_cursors()
        self._agent_event_ids: set[str] = set()
        self._agent_errors: dict[str, str] = {}
        self._agent_snapshot_fingerprint: str | None = None
        self._agent_thread = threading.Thread(target=self._run_agent, daemon=True, name="ga-ink-agent")
        self._agent_thread.start()

    def _run_agent(self) -> None:
        with backend_output_redirect():
            self.agent.run()

    def submit(self, text: str, display_text: str | None = None, images: list | None = None) -> int:
        text = str(text or "")
        if not text.strip() and not images:
            self.emit({"type": "error", "code": "empty_input", "message": "input is empty"})
            return -1
        with self._workflow_planning_lock:
            workflow_planning = self._workflow_planning
        if getattr(self.agent, "is_running", False) or self._is_consuming() or workflow_planning:
            self.emit({"type": "error", "code": "busy", "message": "agent or workflow planner is running"})
            return -1
        if not images:
            from workflow_activation import resolve_workflow_activation

            activation = resolve_workflow_activation(text)
            if activation.action == "recommended":
                return self._submit_recommended_workflow(text, activation)
        if not self._auto_compact_if_needed(text):
            return -1
        self._task_seq += 1
        task_id = self._task_seq
        visible_text = text if display_text is None else str(display_text)
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

    def _submit_recommended_workflow(self, task_text: str, activation) -> int:
        with self._workflow_planning_lock:
            if self._workflow_planning:
                self.emit({"type": "error", "code": "busy", "message": "workflow planner is running"})
                return -1
            self._workflow_planning = True
        self._task_seq += 1
        task_id = self._task_seq
        self._rewind_snapshots[task_id] = self._snapshot_agent_state()
        self._rewind_snapshots[task_id]["text"] = task_text
        self.emit({"type": "user", "taskId": task_id, "text": task_text})
        self.emit({"type": "status", "status": "running", "taskId": task_id})
        self.emit({"type": "activity", "label": "Planning workflow"})
        thread = threading.Thread(
            target=self._run_recommended_workflow,
            args=(task_text, activation),
            daemon=True,
            name=f"ga-ink-workflow-plan-{task_id}",
        )
        self._workflow_planning_thread = thread
        thread.start()
        return task_id

    def _run_recommended_workflow(self, task_text: str, activation) -> None:
        try:
            self.workflow_plan(
                task_text,
                context={"activation": {
                    "mode": activation.mode,
                    "action": activation.action,
                    "confidence": activation.confidence,
                    "matchedSignals": list(activation.matched_signals),
                    "reason": activation.reason,
                    "requiresFanout": activation.requires_fanout,
                    "requiresPhases": activation.requires_phases,
                }},
                auto_approve=True,
                _activation_internal=True,
            )
        finally:
            with self._workflow_planning_lock:
                self._workflow_planning = False

    def stop(self) -> None:
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
            with backend_output_redirect():
                self.agent.abort()
        except Exception:
            pass
        with backend_output_redirect():
            self.agent = self.agent_factory()
            self.agent.inc_out = True
            self.agent.verbose = True
        try:
            runtime = getattr(self.agent, "permission_runtime", None)
            if runtime is not None and hasattr(runtime, "set_emit"):
                runtime.set_emit(self.emit)
        except Exception:
            pass
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
                from mcp_runtime import mcp_status_snapshot

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
                from mcp_runtime import mcp_status_snapshot, start_background_discovery

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
                    # One bounded self-heal pass: a server that lost a slow
                    # handshake gets a second chance instead of showing a failure
                    # marker until the process restarts.
                    recovery_done = True
                    with backend_output_redirect():
                        start_background_discovery(retry_failed=True)
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
                from mcp_runtime import reconnect_mcp_server

                result = reconnect_mcp_server(str(server_name or ""))
            status = result.get("server", {}).get("status", "unknown")
            self.emit({"type": "system", "text": f"MCP server {server_name} reconnect: {status}"})
        except Exception as exc:
            self.emit({"type": "error", "code": "mcp_reconnect_failed", "message": str(exc)})
        self.mcp_status()

    def mcp_enable(self, server_name: str) -> None:
        try:
            with backend_output_redirect():
                from mcp_runtime import enable_mcp_server

                result = enable_mcp_server(str(server_name or ""))
            status = result.get("server", {}).get("status", "unknown")
            self.emit({"type": "system", "text": f"MCP server {server_name} enabled: {status}"})
        except Exception as exc:
            self.emit({"type": "error", "code": "mcp_enable_failed", "message": str(exc)})
        self.mcp_status()

    def mcp_disable(self, server_name: str) -> None:
        try:
            with backend_output_redirect():
                from mcp_runtime import disable_mcp_server

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
                self.emit({"type": "model_switch_result", "ok": True, "message": f"Set model to {result.get('name')}{suffix}"})
            else:
                self.emit({"type": "model_switch_result", "ok": False, "message": str(result.get("message") or "model switch failed")})
        except Exception as exc:
            self.emit({"type": "error", "code": "model_switch_failed", "message": str(exc)})
        self.model_status()

    def permission_status(self) -> None:
        '''上报当前权限档与可选档位列表，供 Ink 面板渲染。'''
        try:
            with backend_output_redirect():
                from permission_policy import (
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
                from skills_runtime import discover_skills

                skills = discover_skills(search_roots=search_roots)
            self.emit(
                {
                    "type": "skill_status",
                    "skills": [
                        {
                            "name": str(skill.name),
                            "description": str(skill.description or ""),
                            "source": str(skill.source or ""),
                            "path": str(skill.path),
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
                from skills_runtime import load_skill_content

                loaded = load_skill_content(str(skill_name or ""), search_roots=search_roots, args=str(args or ""))
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
            with backend_output_redirect():
                result = compact_agent_context(self.agent, instructions=str(instructions or ""))
            if not result.ok:
                self.emit({"type": "local_command_output", "text": f"Compact failed: {result.message}"})
                return
            self._replace_compact_log()
            self._record_compact_transcript(result.message)
            self._rewind_snapshots.clear()
            text = result.message
            # 成功结果只走 history_replace：同一文案再发 local_command_output 会在
            # Ink Static 滚动区留下一份，history_replace 后再打一份 → 用户看到重复。
            # 失败仍用 local_command_output（无 history_replace）。
            self.emit({"type": "history_replace", "messages": [
                {"role": "system", "text": text},
            ]})
        finally:
            self.emit({"type": "activity", "label": None})
            self.emit({"type": "status", "status": "idle"})

    def workflow_plan(
        self,
        task_text: str,
        *,
        context: dict | None = None,
        auto_approve: bool = True,
        args: Any = None,
        timeout_seconds: float | None = None,
        _activation_internal: bool = False,
    ) -> str:
        if getattr(self.agent, "is_running", False) or self._is_consuming() or (self._workflow_planning and not _activation_internal):
            self.emit({"type": "error", "code": "busy", "message": "agent is running"})
            return ""
        task_text = str(task_text or "")
        if not task_text.strip():
            self.emit({"type": "error", "code": "workflow_empty_task", "message": "workflow taskText is required"})
            return ""
        effective_timeout = self._effective_workflow_timeout(timeout_seconds)
        try:
            session_id = str(getattr(self.agent, "session_id", "") or "ink-session")
            with backend_output_redirect():
                planner = self._make_workflow_planner()
                run = self.workflow_controller.create_planned_run(
                    session_id=session_id,
                    task_text=task_text,
                    planner=planner,
                    context=context if isinstance(context, dict) else {},
                    auto_approve=bool(auto_approve),
                    workspace_path=str(self.workspace_root),
                )
            self._emit_agent_read_model()
            self.emit({"type": "workflow_run", "run": self._workflow_run_payload(run)})
            self._emit_workflow_events(run.run_id)
        except Exception as exc:
            self.emit({"type": "error", "code": "workflow_plan_failed", "message": str(exc)})
            self.emit({"type": "activity", "label": None})
            self.emit({"type": "status", "status": "idle"})
            return ""
        if run.status != "running":
            self.emit({"type": "activity", "label": None})
            if run.status in {"succeeded", "degraded", "failed", "cancelled", "killed", "interrupted"}:
                self.emit({"type": "workflow_final", "runId": run.run_id, "result": self._workflow_final_payload(run)})
                if not self._queue_workflow_handoff(run):
                    self.emit({"type": "status", "status": "idle"})
            else:
                self.emit({"type": "status", "status": "idle"})
            return run.run_id
        thread = threading.Thread(
            target=self._run_workflow_runtime,
            args=(run.run_id, args, effective_timeout, None),
            daemon=True,
            name=f"ga-ink-workflow-{run.run_id}",
        )
        self._workflow_threads[run.run_id] = thread
        thread.start()
        return run.run_id

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
                from workflow_models import WorkflowEvent

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

    def workflow_list(self) -> None:
        try:
            runs = self._list_workflow_runs()
            self._emit_agent_read_model()
            self.emit({"type": "workflow_runs", "runs": [self._workflow_run_payload(run) for run in runs]})
        except Exception as exc:
            self.emit({"type": "error", "code": "workflow_list_failed", "message": str(exc)})

    def workflow_detail(self, run_id: str) -> None:
        try:
            with backend_output_redirect():
                run = self.workflow_store.load_run(str(run_id or ""))
                events = self.workflow_store.replay_events(run.run_id)
                draft = self._workflow_artifact_payload(run, run.metadata.get("workflowDraftRef"))
                progress = self._workflow_artifact_payload(run, "workflow-progress.json")
            self._emit_agent_read_model()
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
            self._emit_agent_read_model()
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
        handoff_queued = False
        watch_stop = threading.Event()
        watcher = None
        try:
            self.emit({"type": "status", "status": "running"})
            self.emit({"type": "activity", "label": f"Running workflow {run_id}"})
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
            watch_stop.set()
            self._emit_workflow_events(run_id)
            self.emit({"type": "workflow_run", "run": self._workflow_run_payload(current)})
            self.workflow_progress(run_id)
            self._emit_agent_read_model()
            self.emit({"type": "workflow_final", "runId": run_id, "result": self._workflow_final_payload(current)})
            handoff_queued = self._queue_workflow_handoff(current)
        except Exception as exc:
            watch_stop.set()
            try:
                current = self.workflow_store.load_run(run_id)
                if current.status not in {"succeeded", "degraded", "failed", "killed", "interrupted"}:
                    from workflow_models import WorkflowEvent

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
                self._emit_agent_read_model()
                self.emit({"type": "workflow_final", "runId": run_id, "result": self._workflow_final_payload(current)})
                handoff_queued = self._queue_workflow_handoff(current)
            except Exception:
                pass
            self.emit({"type": "error", "code": "workflow_run_failed", "message": str(exc)})
        finally:
            watch_stop.set()
            self.emit({"type": "activity", "label": None})
            if not handoff_queued:
                self.emit({"type": "status", "status": "idle"})

    def _watch_workflow_progress(self, run_id: str, stop: threading.Event, *, interval: float = 0.25) -> None:
        """Publish workflow-progress snapshots until the run settles.

        ``workflow_progress`` reads a durable file, so polling is safe from a
        background thread; events are already deduplicated by sequence for the
        event stream and progress is a full snapshot, so repeats are harmless.
        """
        last_serialized: str | None = None
        while not stop.wait(interval):
            try:
                with backend_output_redirect():
                    run = self.workflow_store.load_run(run_id)
                    progress = self._workflow_artifact_payload(run, "workflow-progress.json")
            except Exception:
                continue
            if progress is None:
                continue
            try:
                serialized = json.dumps(progress, ensure_ascii=False, sort_keys=True, default=str)
            except Exception:
                continue
            if serialized == last_serialized:
                continue
            last_serialized = serialized
            try:
                self._emit_agent_read_model()
                self.emit({"type": "workflow_progress", "progress": progress})
            except Exception:
                continue
            if str(progress.get("status") or "") in {"succeeded", "degraded", "failed", "cancelled", "killed", "interrupted"}:
                return

    def _make_workflow_planner(self):
        if self.workflow_planner_factory is not None:
            return self.workflow_planner_factory()
        with backend_output_redirect():
            from workflow_planner import build_workflow_planner_from_env
            from workflow_llm import binding_from_agent

            # Prefer main-session /model profile when agent is available.
            agent = self.agent
            try:
                binding = binding_from_agent(agent) if agent is not None else None
            except Exception:
                binding = None
            if binding is not None:
                return build_workflow_planner_from_env(profile_name=binding.profile_name)
            return build_workflow_planner_from_env()

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
            from workflow_child_agent import NativeGPTChildAgentRunner
            from workflow_llm import binding_from_agent
            from workflow_runtime import WorkflowRuntime

            agent = self.agent

            def _binding_provider():
                return binding_from_agent(agent)

            runner = NativeGPTChildAgentRunner(
                binding_provider=_binding_provider,
                enable_tools=True,
            )
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
                    from agent_runtime_models import make_workflow_source_cursor
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

    def _workflow_run_payload(self, run) -> dict[str, Any]:
        data = run.to_dict()
        data.pop("script", None)
        return data

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

        Draft and resumed runs bypass ``create_planned_run``, which is where the
        planned path assigns the per-run workspace. Assigning it here keeps one
        rule for every entry point: a run's deliverables never share a directory
        with another run's.
        """
        from workflow_workspace import create_run_workspace, workspace_metadata

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
        owners: dict[str, list[str]] = {}
        recorded = (run.metadata or {}).get("artifactOwnership")
        if isinstance(recorded, dict):
            for ref, writers in recorded.items():
                if isinstance(writers, (list, tuple)):
                    owners[str(ref)] = [str(writer) for writer in writers]
        for job in run.jobs or []:
            metadata = job.metadata if isinstance(job.metadata, dict) else {}
            for entry in workspace_writes_with_writer(metadata.get('observedArtifacts')):
                writer = entry.get('writer') or str(getattr(job, 'job_id', '') or '')
                if not writer:
                    continue
                bucket = owners.setdefault(entry['path'], [])
                if writer not in bucket:
                    bucket.append(writer)
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
        draft = self._workflow_artifact_payload(run, metadata.get('workflowDraftRef'))
        task_text = str((draft or {}).get('taskText') or '')
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
        if isinstance(collisions, dict) and collisions:
            # Two jobs wrote the same path, so the later write won. The reader
            # must know the file may not represent the earlier job's output.
            handoff['artifactCollisions'] = sanitize(copy.deepcopy(collisions))
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

    def _list_workflow_runs(self):
        root = self.workflow_store.root
        runs = []
        with backend_output_redirect():
            for state_path in root.glob("*/workflows/*/state.json"):
                try:
                    runs.append(self.workflow_store.load_run(state_path.parent.name))
                except Exception:
                    pass
        runs.sort(key=lambda run: str(run.run_id))
        return runs

    def _auto_compact_if_needed(self, pending_text: str) -> bool:
        if compact_agent_context is None or should_auto_compact_agent is None:
            return True
        # 熔断器：连续失败达上限后，本 session 停用自动压缩，避免摘要模型宕机时
        # 每次请求都空打一发（对齐 Claude Code）。放行请求（返回 True），让用户
        # 仍能继续对话（硬裁剪安全网在 llmcore 层仍生效）。
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
            if str(result).startswith("✅"):
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
        elif cmd_type == "workflow_plan":
            raw_timeout = command.get("timeoutSeconds") or command.get("timeout_seconds")
            timeout_seconds = float(raw_timeout) if raw_timeout is not None else None
            auto_approve = command.get("autoApprove")
            if auto_approve is None:
                auto_approve = command.get("auto_approve")
            if auto_approve is None:
                auto_approve = True
            bridge.workflow_plan(
                str(command.get("taskText") or command.get("task_text") or ""),
                context=command.get("context") if isinstance(command.get("context"), dict) else {},
                auto_approve=bool(auto_approve),
                args=command.get("args"),
                timeout_seconds=timeout_seconds,
            )
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
                    from mcp_runtime import reset_mcp_manager

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
