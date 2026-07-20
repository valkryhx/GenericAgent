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


def _backend_log_path() -> str:
    path = os.path.join(PROJECT_DIR, "temp", "ink_bridge_backend.log")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    return path


@contextlib.contextmanager
def backend_output_redirect():
    with open(_backend_log_path(), "a", encoding="utf-8", errors="replace") as log:
        with contextlib.redirect_stdout(log), contextlib.redirect_stderr(log):
            yield


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
        workflow_runtime_factory: Callable[..., Any] | None = None,
        workflow_planner_factory: Callable[[], Any] | None = None,
    ) -> None:
        self.agent_factory = agent_factory
        with backend_output_redirect():
            self.agent = self.agent_factory()
            self.agent.inc_out = True
            self.agent.verbose = True
        raw_emit = emit or make_stdout_emitter(sys.stdout)
        self.emit = lambda event: raw_emit(sanitize(event))
        self._task_seq = 0
        # 自动压缩熔断器：连续失败 N 次后本 session 停用自动压缩，避免摘要模型宕机
        # 时每次请求都空打一发（抄 Claude Code 的 MAX_CONSECUTIVE_AUTOCOMPACT_FAILURES）。
        self._auto_compact_failures = 0
        self._auto_compact_disabled = False
        self._rewind_snapshots: dict[int, dict[str, Any]] = {}
        self._consume_thread: threading.Thread | None = None
        self._workflow_threads: dict[str, threading.Thread] = {}
        self._workflow_emitted_sequences: dict[str, set[int]] = {}
        self.workflow_runtime_factory = workflow_runtime_factory
        self.workflow_planner_factory = workflow_planner_factory
        with backend_output_redirect():
            from workflow_controller import WorkflowController
            from workflow_store import WorkflowStore

        self.workflow_store = WorkflowStore(root=workflow_root)
        self.workflow_controller = WorkflowController(store=self.workflow_store)
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
        if getattr(self.agent, "is_running", False) or self._is_consuming():
            self.emit({"type": "error", "code": "busy", "message": "agent is running"})
            return -1
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
                from mcp_runtime import mcp_status

                payload = mcp_status()
            self.emit({"type": "mcp_status", **payload})
        except Exception as exc:
            self.emit({"type": "error", "code": "mcp_status_failed", "message": str(exc)})

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
                models = [
                    {"index": int(index), "name": str(name), "current": bool(current)}
                    for index, name, current in self.agent.list_llms()
                ]
            self.emit({"type": "model_status", "models": models})
        except Exception as exc:
            self.emit({"type": "error", "code": "model_status_failed", "message": str(exc)})

    def model_switch(self, selector: str) -> None:
        if getattr(self.agent, "is_running", False) or self._is_consuming():
            self.emit({"type": "error", "code": "busy", "message": "agent is running"})
            return
        try:
            with backend_output_redirect():
                result = self.agent.select_llm(str(selector or ""))
            if result.get("ok"):
                self.emit({"type": "model_switch_result", "ok": True, "message": f"Set model to {result.get('name')}"})
            else:
                self.emit({"type": "model_switch_result", "ok": False, "message": str(result.get("message") or "model switch failed")})
        except Exception as exc:
            self.emit({"type": "error", "code": "model_switch_failed", "message": str(exc)})
        self.model_status()

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
            self.emit({"type": "local_command_output", "text": text})
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
    ) -> str:
        if getattr(self.agent, "is_running", False) or self._is_consuming():
            self.emit({"type": "error", "code": "busy", "message": "agent is running"})
            return ""
        task_text = str(task_text or "")
        if not task_text.strip():
            self.emit({"type": "error", "code": "workflow_empty_task", "message": "workflow taskText is required"})
            return ""
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
                )
            self.emit({"type": "workflow_run", "run": self._workflow_run_payload(run)})
            self._emit_workflow_events(run.run_id)
        except Exception as exc:
            self.emit({"type": "error", "code": "workflow_plan_failed", "message": str(exc)})
            return ""
        if run.status != "running":
            return run.run_id
        thread = threading.Thread(
            target=self._run_workflow_runtime,
            args=(run.run_id, args, timeout_seconds, None),
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
            self.emit({"type": "workflow_run", "run": self._workflow_run_payload(run)})
            self._emit_workflow_events(run.run_id)
        except Exception as exc:
            self.emit({"type": "error", "code": "workflow_approve_failed", "message": str(exc)})
            return False
        thread = threading.Thread(
            target=self._run_workflow_runtime,
            args=(run.run_id, args, timeout_seconds, None),
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
                if source.status not in {"succeeded", "failed", "killed", "interrupted"}:
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
                        sequence=max((event.sequence for event in self.workflow_store.replay_events(resumed.run_id)), default=0) + 1,
                        payload={"resumeFromRunId": source_run_id},
                    ),
                )
            self.emit({"type": "workflow_run", "run": self._workflow_run_payload(resumed)})
        except Exception as exc:
            self.emit({"type": "error", "code": "workflow_resume_failed", "message": str(exc)})
            return ""
        thread = threading.Thread(
            target=self._run_workflow_runtime,
            args=(resumed.run_id, args, timeout_seconds, source_run_id),
            daemon=True,
            name=f"ga-ink-workflow-{resumed.run_id}",
        )
        self._workflow_threads[resumed.run_id] = thread
        thread.start()
        return resumed.run_id

    def workflow_list(self) -> None:
        try:
            runs = self._list_workflow_runs()
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
        try:
            self.emit({"type": "status", "status": "running"})
            self.emit({"type": "activity", "label": f"Running workflow {run_id}"})
            with backend_output_redirect():
                run = self.workflow_store.load_run(run_id)
                runtime = self._make_workflow_runtime(timeout_seconds=timeout_seconds)
                runtime.run(run, args=args, resume_from_run_id=resume_from_run_id)
                current = self.workflow_store.load_run(run_id)
            self._emit_workflow_events(run_id)
            self.emit({"type": "workflow_run", "run": self._workflow_run_payload(current)})
            self.workflow_progress(run_id)
            self.emit({"type": "workflow_final", "runId": run_id, "result": self._workflow_final_payload(current)})
        except Exception as exc:
            try:
                current = self.workflow_store.load_run(run_id)
                if current.status not in {"succeeded", "failed", "killed", "interrupted"}:
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
                            sequence=max((event.sequence for event in existing_events), default=0) + 1,
                            payload={"error": reason},
                        ),
                    )
                    current = self.workflow_store.load_run(run_id)
                self._emit_workflow_events(run_id)
                self.emit({"type": "workflow_run", "run": self._workflow_run_payload(current)})
                self.workflow_progress(run_id)
                self.emit({"type": "workflow_final", "runId": run_id, "result": self._workflow_final_payload(current)})
            except Exception:
                pass
            self.emit({"type": "error", "code": "workflow_run_failed", "message": str(exc)})
        finally:
            self.emit({"type": "activity", "label": None})
            self.emit({"type": "status", "status": "idle"})

    def _make_workflow_planner(self):
        if self.workflow_planner_factory is not None:
            return self.workflow_planner_factory()
        with backend_output_redirect():
            from workflow_planner import build_workflow_planner_from_env
        return build_workflow_planner_from_env()

    def _make_workflow_runtime(self, *, timeout_seconds: float | None):
        kwargs = {"store": self.workflow_store}
        if timeout_seconds is not None:
            kwargs["timeout_seconds"] = float(timeout_seconds)
        if self.workflow_runtime_factory is not None:
            return self.workflow_runtime_factory(**kwargs)
        with backend_output_redirect():
            from workflow_runtime import WorkflowRuntime
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
                self.emit({"type": "local_command_output", "text": text})
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
            return {
                "inputTokens": int(usage.get("input_tokens") or 0),
                "outputTokens": int(usage.get("output_tokens") or 0),
                "totalTokens": int(usage.get("total_tokens") or 0),
            }
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
        elif cmd_type == "mcp_reconnect":
            bridge.mcp_reconnect(str(command.get("server") or ""))
        elif cmd_type == "mcp_enable":
            bridge.mcp_enable(str(command.get("server") or ""))
        elif cmd_type == "mcp_disable":
            bridge.mcp_disable(str(command.get("server") or ""))
        elif cmd_type == "model_status":
            bridge.model_status()
        elif cmd_type == "model_switch":
            bridge.model_switch(str(command.get("selector") or ""))
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
