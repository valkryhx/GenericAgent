import json
import os
import signal
import time
from dataclasses import dataclass
from pathlib import Path

from subagent_state import append_jsonl_event, atomic_write_json, now_iso, read_json_or_none, sha256_file


ROUND_END_MARKER = "[ROUND END]"


def normalize_task_name(target):
    target = str(target).strip().replace("\\", "/")
    if not target:
        raise ValueError("target is required")
    if target.startswith("/"):
        target = target.rstrip("/").split("/")[-1]
    if not target.replace("_", "").isalnum() or target.lower() != target:
        raise ValueError("task_name must contain lowercase letters, digits, and underscores")
    return target


@dataclass
class AgentState:
    task_name: str
    agent_path: str
    pid: int | None
    task_dir: str
    turn_status: str
    process_status: str
    round: int
    output_path: str | None
    final_output_path: str | None
    updated_at: str | None = None
    last_message: str | None = None
    last_error: str | None = None


@dataclass
class WaitResult:
    timed_out: bool
    changed_agents: list[AgentState]
    message: str


@dataclass
class CloseResult:
    target: str
    previous_state: AgentState
    closed_state: AgentState
    final_output_path: str | None


@dataclass
class InterruptResult:
    target: str
    previous_state: AgentState
    stop_path: str


@dataclass
class AgentHandle:
    task_name: str
    agent_path: str
    pid: int | None
    task_dir: str
    state_path: str
    command: list[str]


def _default_process_exists(pid):
    if not pid:
        return False
    try:
        import psutil

        return psutil.pid_exists(int(pid))
    except Exception:
        try:
            os.kill(int(pid), 0)
            return True
        except OSError:
            return False


def _default_terminate_process(pid):
    if not pid:
        return
    try:
        import psutil

        psutil.Process(int(pid)).terminate()
    except Exception:
        os.kill(int(pid), signal.SIGTERM)


class SubagentManager:
    def __init__(self, root_dir=None, process_exists=None, terminate_process=None, sleep=None, popen=None, python_executable=None):
        self.root_dir = Path(root_dir or Path(__file__).resolve().parent)
        self.temp_dir = self.root_dir / "temp"
        self.process_exists = process_exists or _default_process_exists
        self.terminate_process = terminate_process or _default_terminate_process
        self.sleep = sleep or time.sleep
        self.popen = popen
        self.python_executable = python_executable or os.environ.get("PYTHON", "python")
        self.repo_dir = Path(__file__).resolve().parent

    def read_agent(self, target):
        task_name = self._task_name_from_target(target)
        task_dir = self.temp_dir / task_name
        state_path = task_dir / "state.json"
        raw = read_json_or_none(state_path) or {}
        refreshed = self._refresh_state(task_name, task_dir, raw)
        atomic_write_json(state_path, refreshed)
        return self._agent_state_from_dict(task_name, task_dir, refreshed)

    def spawn_agent(self, task_name, message, *, llm_no=0, verbose=False, parent_session_id=None, fork_turns="none", fork_history=None):
        task_name = self._task_name_from_target(task_name)
        fork_mode, history_to_write = self._select_fork_history(fork_turns, fork_history)
        task_dir = self.temp_dir / task_name
        task_dir.mkdir(parents=True, exist_ok=True)
        for old_output in task_dir.glob("output*.txt"):
            try:
                old_output.unlink()
            except OSError:
                pass
        (task_dir / "input.txt").write_text(message, encoding="utf-8")
        old_history = task_dir / "_history.json"
        if old_history.exists():
            old_history.unlink()
        if history_to_write is not None:
            atomic_write_json(old_history, history_to_write)
        state_path = task_dir / "state.json"
        state = {
            "schema_version": 1,
            "task_name": task_name,
            "agent_path": f"/root/{task_name}",
            "parent_session_id": parent_session_id,
            "pid": None,
            "round": 0,
            "turn_status": "pending",
            "process_status": "starting",
            "started_at": now_iso(),
            "updated_at": now_iso(),
            "input_path": str(task_dir / "input.txt"),
            "output_path": str(task_dir / "output.txt"),
            "final_output_path": None,
            "final_output_sha256": None,
            "last_message": message,
            "last_error": None,
            "close_reason": None,
            "fork_turns": fork_mode,
        }
        atomic_write_json(state_path, state)
        append_jsonl_event(task_dir / "events.jsonl", {"type": "agent_started", "task_name": task_name, "parent_session_id": parent_session_id})
        cmd = [
            self.python_executable,
            str(self.repo_dir / "agentmain.py"),
            "--task",
            task_name,
            "--nobg",
            "--task_root",
            str(self.root_dir),
            "--llm_no",
            str(llm_no),
        ]
        if verbose:
            cmd.append("--verbose")
        stdout = open(task_dir / "stdout.log", "w", encoding="utf-8")
        stderr = open(task_dir / "stderr.log", "w", encoding="utf-8")
        try:
            kwargs = {"cwd": str(self.root_dir), "stdout": stdout, "stderr": stderr}
            if os.name == "nt":
                kwargs["creationflags"] = 0x08000000
            proc = (self.popen or __import__("subprocess").Popen)(cmd, **kwargs)
        finally:
            stdout.close()
            stderr.close()
        pid = getattr(proc, "pid", None)
        state.update({"pid": pid, "process_status": "alive", "updated_at": now_iso()})
        atomic_write_json(state_path, state)
        self._write_registry_entry(task_name, state, task_dir)
        return AgentHandle(task_name, state["agent_path"], pid, str(task_dir), str(state_path), cmd)

    def register_agent(self, task_name, state, task_dir=None):
        task_name = self._task_name_from_target(task_name)
        task_dir = Path(task_dir) if task_dir is not None else self.temp_dir / task_name
        task_dir.mkdir(parents=True, exist_ok=True)
        self._write_registry_entry(task_name, state, task_dir)
        return self._registry_entry(task_name, state, task_dir)

    def list_agents(self, path_prefix=None, include_closed=False):
        if not self.temp_dir.exists():
            return []
        states = []
        for child in sorted(self.temp_dir.iterdir()):
            if not child.is_dir() or child.name == "subagents":
                continue
            if not (child / "state.json").is_file():
                continue
            try:
                self._task_name_from_target(child.name)
                state = self.read_agent(child.name)
            except (FileNotFoundError, ValueError):
                continue
            if path_prefix and not state.agent_path.startswith(path_prefix):
                continue
            if not include_closed and state.process_status in {"shutdown", "killed"}:
                continue
            states.append(state)
        return states

    def send_message(self, target, message, *, author="/root"):
        return self._queue_message(target, message, author=author, trigger_turn=False)

    def followup_task(self, target, message, *, author="/root"):
        result = self._queue_message(target, message, author=author, trigger_turn=True)
        task_dir = Path(self.read_agent(target).task_dir)
        (task_dir / "reply.txt").write_text(message, encoding="utf-8")
        return result

    def wait_agents(self, targets=None, timeout_s=30, poll_interval_s=0.5, since_event_offsets=None):
        deadline = time.monotonic() + timeout_s
        if targets is None:
            targets = [state.task_name for state in self.list_agents()]
        else:
            targets = [self._task_name_from_target(target) for target in targets if str(target).strip()]
        if not targets:
            return WaitResult(True, [], "No subagents to wait for.")
        baseline = since_event_offsets or {target: self._event_size(target) for target in targets}
        while True:
            changed = []
            for target in targets:
                state = self.read_agent(target)
                if self._event_size(target) != baseline.get(target, 0) or self._is_notify_state(state):
                    changed.append(state)
            if changed:
                return WaitResult(False, changed, "Wait completed.")
            if time.monotonic() >= deadline:
                return WaitResult(True, [], "Wait timed out.")
            self.sleep(poll_interval_s)

    def interrupt_agent(self, target, reason="parent_interrupt"):
        previous = self.read_agent(target)
        task_dir = Path(previous.task_dir)
        task_dir.mkdir(parents=True, exist_ok=True)
        stop_path = task_dir / "_stop"
        stop_path.write_text(reason, encoding="utf-8")
        append_jsonl_event(
            task_dir / "events.jsonl",
            {
                "type": "interrupt_requested",
                "task_name": previous.task_name,
                "reason": reason,
            },
        )
        return InterruptResult(target, previous, str(stop_path))

    def close_agent(self, target, reason="parent_cleanup", grace_s=2.0):
        previous = self.read_agent(target)
        task_dir = Path(previous.task_dir)
        (task_dir / "_stop").write_text(reason, encoding="utf-8")
        append_jsonl_event(
            task_dir / "events.jsonl",
            {
                "type": "stop_requested",
                "task_name": previous.task_name,
                "reason": reason,
            },
        )
        close_process_status = "shutdown"
        if previous.pid and self.process_exists(previous.pid):
            if grace_s > 0:
                self.sleep(grace_s)
            if self.process_exists(previous.pid):
                self.terminate_process(previous.pid)
                close_process_status = "killed"

        closed_turn_status = previous.turn_status
        if closed_turn_status not in {"completed", "errored"}:
            closed_turn_status = "interrupted"
        raw = read_json_or_none(task_dir / "state.json") or {}
        raw.update(
            {
                "schema_version": 1,
                "task_name": previous.task_name,
                "agent_path": previous.agent_path,
                "pid": previous.pid,
                "round": previous.round,
                "turn_status": closed_turn_status,
                "process_status": close_process_status,
                "output_path": previous.output_path,
                "final_output_path": previous.final_output_path,
                "updated_at": now_iso(),
                "close_reason": reason,
            }
        )
        atomic_write_json(task_dir / "state.json", raw)
        append_jsonl_event(
            task_dir / "events.jsonl",
            {
                "type": "agent_closed",
                "task_name": previous.task_name,
                "previous_turn_status": previous.turn_status,
                "previous_process_status": previous.process_status,
                "closed_process_status": close_process_status,
                "reason": reason,
            },
        )
        closed = self._agent_state_from_dict(previous.task_name, task_dir, raw)
        return CloseResult(target, previous, closed, previous.final_output_path)

    def _queue_message(self, target, message, *, author, trigger_turn):
        task_name = self._task_name_from_target(target)
        task_dir = self.temp_dir / task_name
        task_dir.mkdir(parents=True, exist_ok=True)
        mailbox_path = task_dir / "mailbox.jsonl"
        row = {
            "schema_version": 1,
            "id": f"msg_{int(time.time() * 1000)}_{len(mailbox_path.read_text(encoding='utf-8').splitlines()) if mailbox_path.exists() else 0}",
            "author": author,
            "recipient": f"/root/{task_name}",
            "content": message,
            "trigger_turn": trigger_turn,
            "priority": "normal",
            "created_at": now_iso(),
            "consumed_at": None,
        }
        append_jsonl_event(mailbox_path, row)
        append_jsonl_event(
            task_dir / "events.jsonl",
            {
                "type": "message_queued",
                "task_name": task_name,
                "author": author,
                "trigger_turn": trigger_turn,
            },
        )
        return row

    def _write_registry_entry(self, task_name, state, task_dir):
        registry_path = self.temp_dir / "subagents" / "registry.json"
        registry = read_json_or_none(registry_path) or {"schema_version": 1, "agents": {}}
        registry.setdefault("schema_version", 1)
        registry.setdefault("agents", {})
        registry["updated_at"] = now_iso()
        registry["agents"][f"/root/{task_name}"] = self._registry_entry(task_name, state, task_dir)
        atomic_write_json(registry_path, registry)

    def _registry_entry(self, task_name, state, task_dir):
        return {
            "task_name": task_name,
            "agent_path": f"/root/{task_name}",
            "parent_path": "/root",
            "parent_session_id": state.get("parent_session_id"),
            "pid": state.get("pid"),
            "task_dir": str(task_dir),
            "state_path": str(task_dir / "state.json"),
            "last_task_message": state.get("last_message"),
            "created_at": state.get("started_at"),
            "closed_at": None,
        }

    def _task_name_from_target(self, target):
        return normalize_task_name(target)

    def _select_fork_history(self, fork_turns, fork_history):
        mode = str(fork_turns or "").strip().lower()
        if mode == "none":
            return "none", None
        if mode == "all":
            if fork_history is None:
                raise ValueError("fork_history is required when fork_turns is not 'none'")
            return "all", list(fork_history)
        try:
            n = int(mode)
        except (TypeError, ValueError):
            raise ValueError("fork_turns must be 'none', 'all', or a positive integer string")
        if n <= 0:
            raise ValueError("fork_turns must be 'none', 'all', or a positive integer string")
        if fork_history is None:
            raise ValueError("fork_history is required when fork_turns is not 'none'")
        return str(n), list(fork_history)[-n:]

    def _event_size(self, target):
        task_name = self._task_name_from_target(target)
        path = self.temp_dir / task_name / "events.jsonl"
        try:
            return path.stat().st_size
        except FileNotFoundError:
            return 0

    def _is_notify_state(self, state):
        return state.turn_status in {"completed", "errored", "interrupted"} or state.process_status in {
            "waiting_reply",
            "exited",
            "shutdown",
            "killed",
        }

    def _refresh_state(self, task_name, task_dir, raw):
        task_dir.mkdir(parents=True, exist_ok=True)
        output_path = self._resolve_output_path(task_dir, raw)
        has_round_end = self._has_round_end(output_path)
        pid = raw.get("pid")
        try:
            pid = int(pid) if pid is not None else None
        except (TypeError, ValueError):
            pid = None
        alive = self.process_exists(pid) if pid else False

        turn_status = raw.get("turn_status") or "pending"
        process_status = raw.get("process_status") or "not_found"
        final_output_path = raw.get("final_output_path")
        final_output_sha256 = raw.get("final_output_sha256")

        terminal_process_statuses = {"shutdown", "killed"}
        preserve_terminal_process_status = process_status in terminal_process_statuses and not alive

        if has_round_end:
            turn_status = "completed"
            final_output_path = str(output_path)
            try:
                final_output_sha256 = sha256_file(output_path)
            except OSError:
                final_output_sha256 = raw.get("final_output_sha256")
            if alive:
                process_status = "waiting_reply"
            elif preserve_terminal_process_status:
                process_status = raw.get("process_status")
            else:
                process_status = "exited"
        else:
            if alive:
                process_status = "alive"
                if turn_status in {"pending", "completed"}:
                    turn_status = "running"
            elif preserve_terminal_process_status:
                process_status = raw.get("process_status")
            else:
                process_status = "exited"
                if turn_status == "pending":
                    turn_status = "running"

        refreshed = dict(raw)
        refreshed.update(
            {
                "schema_version": 1,
                "task_name": task_name,
                "agent_path": raw.get("agent_path") or f"/root/{task_name}",
                "pid": pid,
                "round": int(raw.get("round") or 0),
                "turn_status": turn_status,
                "process_status": process_status,
                "output_path": str(output_path) if output_path else raw.get("output_path"),
                "final_output_path": final_output_path,
                "final_output_sha256": final_output_sha256,
                "updated_at": now_iso(),
            }
        )
        return refreshed

    def _resolve_output_path(self, task_dir, raw):
        path = raw.get("output_path")
        if path:
            path = Path(path)
            if not path.is_absolute():
                path = self.root_dir / path
            return path
        outputs = sorted(task_dir.glob("output*.txt"), key=lambda p: p.stat().st_mtime, reverse=True)
        return outputs[0] if outputs else None

    def _has_round_end(self, output_path):
        if not output_path:
            return False
        try:
            return ROUND_END_MARKER in Path(output_path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return False

    def _agent_state_from_dict(self, task_name, task_dir, raw):
        return AgentState(
            task_name=raw.get("task_name") or task_name,
            agent_path=raw.get("agent_path") or f"/root/{task_name}",
            pid=raw.get("pid"),
            task_dir=str(task_dir),
            turn_status=raw.get("turn_status") or "pending",
            process_status=raw.get("process_status") or "not_found",
            round=int(raw.get("round") or 0),
            output_path=raw.get("output_path"),
            final_output_path=raw.get("final_output_path"),
            updated_at=raw.get("updated_at"),
            last_message=raw.get("last_message"),
            last_error=raw.get("last_error"),
        )


_DEFAULT_MANAGER = SubagentManager()


def read_agent(target):
    return _DEFAULT_MANAGER.read_agent(target)


def list_agents(path_prefix=None, include_closed=False):
    return _DEFAULT_MANAGER.list_agents(path_prefix, include_closed=include_closed)


def register_agent(task_name, state, task_dir=None):
    return _DEFAULT_MANAGER.register_agent(task_name, state, task_dir)


def wait_agents(targets=None, timeout_s=30, poll_interval_s=0.5, since_event_offsets=None):
    return _DEFAULT_MANAGER.wait_agents(targets, timeout_s, poll_interval_s, since_event_offsets)


def close_agent(target, reason="parent_cleanup", grace_s=2.0):
    return _DEFAULT_MANAGER.close_agent(target, reason=reason, grace_s=grace_s)


def interrupt_agent(target, reason="parent_interrupt"):
    return _DEFAULT_MANAGER.interrupt_agent(target, reason=reason)


def spawn_agent(task_name, message, *, llm_no=0, verbose=False, parent_session_id=None, fork_turns="none", fork_history=None):
    return _DEFAULT_MANAGER.spawn_agent(
        task_name,
        message,
        llm_no=llm_no,
        verbose=verbose,
        parent_session_id=parent_session_id,
        fork_turns=fork_turns,
        fork_history=fork_history,
    )


def send_message(target, message, *, author="/root"):
    return _DEFAULT_MANAGER.send_message(target, message, author=author)


def followup_task(target, message, *, author="/root"):
    return _DEFAULT_MANAGER.followup_task(target, message, author=author)
