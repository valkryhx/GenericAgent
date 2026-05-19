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
            stdout.write(encode_event(event))
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


class GenericAgentBridge:
    def __init__(self, agent_factory: AgentFactory = default_agent_factory, emit: EmitFn | None = None) -> None:
        with backend_output_redirect():
            self.agent = agent_factory()
            self.agent.inc_out = True
            self.agent.verbose = True
        self.emit = emit or make_stdout_emitter(sys.stdout)
        self._task_seq = 0
        self._rewind_snapshots: dict[int, dict[str, Any]] = {}
        self._consume_thread: threading.Thread | None = None
        self._agent_thread = threading.Thread(target=self._run_agent, daemon=True, name="ga-ink-agent")
        self._agent_thread.start()

    def _run_agent(self) -> None:
        with backend_output_redirect():
            self.agent.run()

    def submit(self, text: str, display_text: str | None = None) -> int:
        text = str(text or "")
        if not text.strip():
            self.emit({"type": "error", "code": "empty_input", "message": "input is empty"})
            return -1
        if getattr(self.agent, "is_running", False) or self._is_consuming():
            self.emit({"type": "error", "code": "busy", "message": "agent is running"})
            return -1
        self._task_seq += 1
        task_id = self._task_seq
        visible_text = text if display_text is None else str(display_text)
        self._rewind_snapshots[task_id] = self._snapshot_agent_state()
        self._rewind_snapshots[task_id]["text"] = visible_text
        self.emit({"type": "user", "taskId": task_id, "text": visible_text})
        self.emit({"type": "status", "status": "running", "taskId": task_id})
        try:
            display_queue = self.agent.put_task(text, source="user")
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
        else:
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
                self.emit({"type": "system", "text": f"Set model to {result.get('name')}"})
            else:
                self.emit({"type": "system", "text": str(result.get("message") or "model switch failed")})
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

    def list_resume_sessions(self) -> None:
        if continue_list is None:
            self.emit({"type": "error", "code": "resume_unavailable", "message": "/resume is unavailable"})
            return
        try:
            sessions = continue_list(exclude_pid=os.getpid())
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
            sessions = continue_list(exclude_pid=os.getpid())
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
        for stale_id in [key for key in self._rewind_snapshots if key > task_id]:
            del self._rewind_snapshots[stale_id]
        self.emit({"type": "rewind_done", "taskId": task_id, "text": str(snapshot.get("text") or "")})

    def _snapshot_agent_state(self) -> dict[str, Any]:
        return {
            "text": "",
            "history": copy.deepcopy(getattr(self.agent, "history", [])),
            "backend_history": copy.deepcopy(self._backend_history()),
            "last_tools": copy.deepcopy(getattr(getattr(self.agent, "llmclient", None), "last_tools", "")),
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

    def _backend_history(self) -> Any:
        backend = getattr(getattr(self.agent, "llmclient", None), "backend", None)
        return getattr(backend, "history", [])

    def _resume_ui_messages_with_checkpoints(self, path: str) -> list[dict[str, Any]]:
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
        try:
            while True:
                item = display_queue.get()
                if "next" in item:
                    self.emit({"type": "assistant_delta", "taskId": task_id, "text": str(item.get("next") or "")})
                if "done" in item:
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
            bridge.submit(str(command.get("text") or ""))
        elif cmd_type == "stop":
            bridge.stop()
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
