import json
import os
import signal
import time
from dataclasses import dataclass, field
from pathlib import Path

from subagent_agent_path import AgentPath
from subagent_artifacts import SubagentArtifactStore
from subagent_event_bus import SubagentEventBus
from subagent_mailbox import QUEUE_ONLY, TRIGGER_TURN, SubagentMailbox
from subagent_permissions import INHERIT_CURRENT_PERMISSIONS, normalize_permission_metadata
from subagent_registry import SubagentRegistry
from subagent_transcript import SubagentTranscriptStore
from subagent_state import append_jsonl_event, append_parent_inbox_event, atomic_write_json, now_iso, read_json_or_none, sha256_file


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
    parent_session_id: str | None = None
    run_id: str | None = None
    artifact_dir: str | None = None
    permission_profile: str | None = None
    parent_permission_mode: str | None = None
    permission_options: dict | None = None
    agent_type: str | None = None
    role_source_path: str | None = None
    background: bool = True
    ipc_mode: str | None = None
    effective_ipc_mode: str | None = None
    ipc_fallback_reason: str | None = None
    isolation: str | None = None
    worktree_path: str | None = None
    handoff_mode: str | None = None
    handoff_reason: str | None = None
    worktree_summary: dict | None = None
    worktree_cleanup: dict | None = None
    attach_status: str | None = None
    ipc_endpoint: dict | None = None
    close_reason: str | None = None


@dataclass
class HandoffResult:
    target: str
    previous_state: AgentState
    updated_state: AgentState
    handoff_mode: str
    reason: str


@dataclass
class AttachResult:
    target: str
    state: AgentState
    handoff_mode: str
    attach_status: str
    reason: str
    output_path: str | None
    stream_text: str
    stream_offset: int
    next_stream_offset: int
    stream_truncated: bool
    stream_eof: bool
    next_event_seq: int | None = None


@dataclass
class WaitResult:
    timed_out: bool
    changed_agents: list[AgentState]
    message: str
    events: list[dict] | None = None
    next_event_seq: int | None = None


@dataclass
class CloseResult:
    target: str
    previous_state: AgentState
    closed_state: AgentState
    final_output_path: str | None
    closed_descendants: list[dict] = field(default_factory=list)


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
    run_id: str | None = None
    artifact_dir: str | None = None
    permission_profile: str | None = None
    parent_permission_mode: str | None = None
    permission_options: dict | None = None
    agent_type: str | None = None
    role_source_path: str | None = None
    background: bool = True
    ipc_mode: str | None = None
    effective_ipc_mode: str | None = None
    ipc_fallback_reason: str | None = None
    isolation: str | None = None
    worktree_path: str | None = None
    handoff_mode: str | None = None
    handoff_reason: str | None = None
    worktree_summary: dict | None = None
    worktree_cleanup: dict | None = None
    ipc_endpoint: dict | None = None


@dataclass
class ResumeResult:
    target: str
    previous_state: AgentState
    handle: AgentHandle
    resume_context: dict


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
    def __init__(self, root_dir=None, process_exists=None, terminate_process=None, sleep=None, popen=None, python_executable=None, worktree_creator=None, worktree_runner=None, realtime_channel_factory=None, self_agent_path=None):
        self.root_dir = Path(root_dir or Path(__file__).resolve().parent)
        self.temp_dir = self.root_dir / "temp"
        from subagent_registry import resolve_tree_limits_from_env

        self.registry = SubagentRegistry(
            self.temp_dir / "subagents",
            process_exists=process_exists or _default_process_exists,
            **resolve_tree_limits_from_env(),
        )

        # Which agent this manager speaks for. A child process runs the same code against the
        # same registry, so without this every spawn would register under /root and a deep
        # tree would look flat to the depth guard.
        self.self_agent_path = self._resolve_self_agent_path(self_agent_path)
        self.realtime_channel_factory = realtime_channel_factory or self._env_realtime_channel_factory()
        self._realtime_channels = {}
        self.event_bus = SubagentEventBus(self.temp_dir / "subagents", publisher=self._publish_realtime_event)
        self.process_exists = process_exists or _default_process_exists
        self.terminate_process = terminate_process or _default_terminate_process
        self.sleep = sleep or time.sleep
        self.popen = popen
        self.python_executable = python_executable or os.environ.get("PYTHON", "python")
        self.worktree_creator = worktree_creator
        self.worktree_runner = worktree_runner
        self.repo_dir = Path(__file__).resolve().parent

    AGENT_PATH_ENV = "GA_SUBAGENT_AGENT_PATH"

    def _resolve_self_agent_path(self, self_agent_path):
        raw = self_agent_path or os.environ.get(self.AGENT_PATH_ENV)
        if not raw:
            return AgentPath.root()
        try:
            return AgentPath.parse(str(raw))
        except ValueError:
            return AgentPath.root()

    def _env_realtime_channel_factory(self):
        """Realtime IPC stays opt-in; without it the durable file bus remains the transport."""
        if os.environ.get("GA_SUBAGENT_REALTIME_IPC") != "1":
            return None

        def factory(run_id, task_name):
            from subagent_realtime_ipc import SubagentRealtimeChannel, default_channel_address, new_channel_authkey

            channel_dir = self.temp_dir / "subagents" / "channels"
            # Per-run key, because the address is derived from a sequential run_id and is
            # therefore guessable by any local process.
            return SubagentRealtimeChannel(default_channel_address(channel_dir, run_id), authkey=new_channel_authkey())

        return factory

    def _open_realtime_ipc(self, ipc_mode, *, run_id, task_name, agent_path):
        from subagent_ipc import normalize_ipc_metadata

        factory = self.realtime_channel_factory
        channel_factory = (lambda: factory(run_id, task_name)) if factory is not None else None
        metadata = normalize_ipc_metadata(ipc_mode, channel_factory=channel_factory)
        channel = metadata.pop("channel", None)
        if channel is not None:
            self._realtime_channels[str(agent_path)] = channel
        return metadata

    def _write_realtime_authkey(self, agent_path, task_dir):
        """Deliver the channel key to the child out-of-band, after its task dir exists."""
        channel = self._realtime_channels.get(str(agent_path or ""))
        authkey = getattr(channel, "authkey", None) if channel is not None else None
        if not authkey:
            return None
        from subagent_realtime_ipc import write_channel_authkey

        try:
            return write_channel_authkey(task_dir, authkey)
        except OSError:
            return None

    def _publish_realtime_event(self, event):
        channel = self._realtime_channels.get(str(event.get("agent_path") or ""))
        if channel is None:
            return
        channel.publish(event)

    def _close_realtime_channel(self, agent_path, task_dir=None):
        channel = self._realtime_channels.pop(str(agent_path or ""), None)
        if task_dir is not None:
            from subagent_realtime_ipc import remove_channel_authkey

            remove_channel_authkey(task_dir)
        if channel is None:
            return
        try:
            channel.close()
        except Exception:
            pass

    def _child_popen_kwargs(self, worktree_path, stdout, stderr, agent_path):
        """Launch kwargs for a child agentmain.py process.

        The child runs the same code against the same registry, so it has to be told which
        agent it is; otherwise its own spawns would register under /root and the depth guard
        would see a flat tree.
        """
        kwargs = {
            "cwd": str(worktree_path or self.root_dir),
            "stdout": stdout,
            "stderr": stderr,
            "env": {**os.environ, self.AGENT_PATH_ENV: str(agent_path)},
        }
        if os.name == "nt":
            kwargs["creationflags"] = 0x08000000
        return kwargs

    def _create_child_or_reject(self, *, task_name, task_dir, state_path, parent_session_id, message, permission_metadata, **entry_fields):
        """Register the child, or record why the tree guard refused it before re-raising.

        A refused spawn that leaves no trace is indistinguishable from one that was never
        attempted, which is exactly what makes runaway-spawn incidents hard to reconstruct.
        """
        from subagent_registry import SubagentTreeLimitError

        try:
            return self.registry.create_child(
                parent_path=self.self_agent_path,
                task_name=task_name,
                task_dir=task_dir,
                state_path=state_path,
                parent_session_id=parent_session_id,
                last_task_message=message,
                parent_permission_mode=permission_metadata.get("parent_permission_mode"),
                permission_profile=permission_metadata["permission_profile"],
                permission_options=permission_metadata["options"],
                **entry_fields,
            )
        except SubagentTreeLimitError as e:
            self.event_bus.append_event(
                "spawn_rejected",
                agent_path=str(self.self_agent_path),
                task_name=task_name,
                payload={
                    "reason": str(e),
                    "parent_agent_path": str(self.self_agent_path),
                    "max_depth": self.registry.max_depth,
                    "max_active_agents": self.registry.max_active_agents,
                },
                notify=True,
            )
            raise

    def read_agent(self, target):
        task_name = self._task_name_from_target(target)
        task_dir = self.temp_dir / task_name
        state_path = task_dir / "state.json"
        raw = read_json_or_none(state_path) or {}
        refreshed = self._refresh_state(task_name, task_dir, raw)
        atomic_write_json(state_path, refreshed)
        self._write_registry_entry(task_name, refreshed, task_dir)
        return self._agent_state_from_dict(task_name, task_dir, refreshed)

    def probe_agent(self, target):
        """Same derived status as ``read_agent``, without writing anything.

        ``read_agent`` persists on every call, so a wait loop polling four agents produced 20
        state.json + 20 registry.json atomic writes per second — and those registry writes were
        the main feeder of the M5 race (docs/ga_subagent_control_plane_defects_2026-07-30.md §3).
        Observing must not write; the write only happens once a wait actually has something to
        report, via read_agent.
        """
        task_name = self._task_name_from_target(target)
        task_dir = self.temp_dir / task_name
        raw = read_json_or_none(task_dir / "state.json") or {}
        refreshed = self._refresh_state(task_name, task_dir, raw, persist_side_effects=False)
        return self._agent_state_from_dict(task_name, task_dir, refreshed)

    def spawn_agent(
        self,
        task_name,
        message,
        *,
        llm_no=0,
        verbose=False,
        parent_session_id=None,
        fork_turns="none",
        fork_history=None,
        permission_profile=INHERIT_CURRENT_PERMISSIONS,
        parent_permission_mode=None,
        permission_options=None,
        agent_type=None,
        role_source_path=None,
        background=True,
        ipc_mode="file",
        isolation=None,
        worktree_path=None,
    ):
        task_name = self._next_available_task_name(self._task_name_from_target(task_name))
        permission_metadata = normalize_permission_metadata(
            {"permission_profile": permission_profile, "permission_options": permission_options or {}, "parent_permission_mode": parent_permission_mode}
        )
        fork_mode, history_to_write = self._select_fork_history(fork_turns, fork_history)
        requested_ipc_mode = str(ipc_mode or "file").strip().lower() or "file"
        ipc_mode = requested_ipc_mode
        effective_ipc_mode = "file"
        ipc_fallback_reason = None
        ipc_endpoint = None
        task_dir = self.temp_dir / task_name
        state_path = task_dir / "state.json"
        registry_entry = self._create_child_or_reject(
            task_name=task_name,
            task_dir=task_dir,
            state_path=state_path,
            parent_session_id=parent_session_id,
            message=message,
            permission_metadata=permission_metadata,
            agent_type=agent_type,
            role_source_path=role_source_path,
            background=background,
            ipc_mode=ipc_mode,
            effective_ipc_mode=effective_ipc_mode,
            ipc_fallback_reason=ipc_fallback_reason,
            isolation=isolation,
            worktree_path=worktree_path,
        )
        ipc_metadata = self._open_realtime_ipc(
            requested_ipc_mode,
            run_id=registry_entry.run_id,
            task_name=registry_entry.task_name,
            agent_path=registry_entry.agent_path,
        )
        ipc_mode = ipc_metadata["ipc_mode"]
        effective_ipc_mode = ipc_metadata["effective_ipc_mode"]
        ipc_fallback_reason = ipc_metadata["ipc_fallback_reason"]
        ipc_endpoint = ipc_metadata.get("ipc_endpoint")
        self.registry.update(
            registry_entry.agent_path,
            ipc_mode=ipc_mode,
            effective_ipc_mode=effective_ipc_mode,
            ipc_fallback_reason=ipc_fallback_reason,
        )
        task_name = registry_entry.task_name
        task_dir = Path(registry_entry.task_dir)
        state_path = Path(registry_entry.state_path)
        if isolation == "worktree" and not worktree_path:
            from subagent_worktree import create_subagent_worktree
            creator = self.worktree_creator or create_subagent_worktree
            try:
                worktree = creator(self.root_dir, self.temp_dir / "subagents" / "worktrees", registry_entry.run_id)
                worktree_path = worktree.get("path")
            except Exception as e:
                error = f"{type(e).__name__}: {e}"
                task_dir.mkdir(parents=True, exist_ok=True)
                state = {
                    "schema_version": 1,
                    "task_name": task_name,
                    "agent_path": str(registry_entry.agent_path),
                    "parent_session_id": parent_session_id,
                    "run_id": registry_entry.run_id,
                    "artifact_dir": registry_entry.artifact_dir,
                    "permission_profile": permission_metadata["permission_profile"],
                    "parent_permission_mode": permission_metadata.get("parent_permission_mode"),
                    "permission_options": permission_metadata["options"],
                    "llm_no": llm_no,
                    "verbose": bool(verbose),
                    "agent_type": agent_type,
                    "role_source_path": role_source_path,
                    "background": bool(background),
                    "handoff_mode": "background" if background else "foreground",
                    "handoff_reason": "spawn",
                    "ipc_mode": ipc_mode,
                    "effective_ipc_mode": effective_ipc_mode,
                    "ipc_fallback_reason": ipc_fallback_reason,
                    "ipc_endpoint": ipc_endpoint,
                    "isolation": isolation,
                    "worktree_path": None,
                    "pid": None,
                    "round": 0,
                    "turn_status": "errored",
                    "process_status": "exited",
                    "started_at": now_iso(),
                    "updated_at": now_iso(),
                    "input_path": str(task_dir / "input.txt"),
                    "output_path": str(task_dir / "output.txt"),
                    "final_output_path": None,
                    "final_output_sha256": None,
                    "last_message": message,
                    "last_error": error,
                    "close_reason": "worktree_error",
                    "fork_turns": fork_mode,
                    **self._fork_metadata(fork_mode, history_to_write),
                }
                atomic_write_json(state_path, state)
                event = {
                    "type": "agent_error",
                    "task_name": task_name,
                    "parent_session_id": parent_session_id,
                    "permission_profile": permission_metadata["permission_profile"],
                    "error": error,
                    "agent_type": agent_type,
                }
                append_jsonl_event(task_dir / "events.jsonl", event)
                append_parent_inbox_event(task_dir, event)
                bus_event = self.event_bus.append_event(
                    "agent_error",
                    agent_path=state.get("agent_path"),
                    run_id=state.get("run_id"),
                    task_name=task_name,
                    status={"turn_status": state.get("turn_status"), "process_status": state.get("process_status")},
                    payload={"error": error, "agent_type": agent_type},
                    notify=True,
                )
                state["last_event_seq"] = bus_event["event_seq"]
                atomic_write_json(state_path, state)
                self._write_registry_entry(task_name, state, task_dir)
                self.registry.mark_closed(registry_entry.agent_path, previous_status="starting", closed_status="worktree_error")
                raise
        task_dir.mkdir(parents=True, exist_ok=True)
        self._write_realtime_authkey(registry_entry.agent_path, task_dir)
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
        fork_metadata = self._fork_metadata(fork_mode, history_to_write)
        state = {
            "schema_version": 1,
            "task_name": task_name,
            "agent_path": str(registry_entry.agent_path),
            "parent_session_id": parent_session_id,
            "run_id": registry_entry.run_id,
            "artifact_dir": registry_entry.artifact_dir,
            "permission_profile": permission_metadata["permission_profile"],
            "parent_permission_mode": permission_metadata.get("parent_permission_mode"),
            "permission_options": permission_metadata["options"],
            "llm_no": llm_no,
            "verbose": bool(verbose),
            "agent_type": agent_type,
            "role_source_path": role_source_path,
            "background": bool(background),
            "handoff_mode": "background" if background else "foreground",
            "handoff_reason": "spawn",
            "ipc_mode": ipc_mode,
            "effective_ipc_mode": effective_ipc_mode,
            "ipc_fallback_reason": ipc_fallback_reason,
            "ipc_endpoint": ipc_endpoint,
            "isolation": isolation,
            "worktree_path": worktree_path,
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
            **fork_metadata,
        }
        atomic_write_json(state_path, state)
        if parent_session_id:
            try:
                SubagentTranscriptStore(self.temp_dir / "sessions").write_metadata(
                    session_id=parent_session_id,
                    run_id=registry_entry.run_id,
                    agent_path=str(registry_entry.agent_path),
                    task_name=task_name,
                    permission_profile=permission_metadata["permission_profile"],
                    agent_type=agent_type,
                )
            except Exception:
                pass
        agentmain_path = Path(worktree_path) / "agentmain.py" if isolation == "worktree" and worktree_path else self.repo_dir / "agentmain.py"
        cmd = [
            self.python_executable,
            str(agentmain_path),
            "--task",
            task_name,
            "--nobg",
            "--task_root",
            str(self.root_dir),
            "--llm_no",
            str(llm_no),
            "--permission_profile",
            permission_metadata["permission_profile"],
            "--parent_permission_mode",
            permission_metadata.get("parent_permission_mode") or "",
            "--permission_options",
            json.dumps(permission_metadata["options"], ensure_ascii=False, sort_keys=True),
        ]
        if verbose:
            cmd.append("--verbose")
        stdout = open(task_dir / "stdout.log", "w", encoding="utf-8")
        stderr = open(task_dir / "stderr.log", "w", encoding="utf-8")
        try:
            kwargs = self._child_popen_kwargs(worktree_path, stdout, stderr, registry_entry.agent_path)
            proc = (self.popen or __import__("subprocess").Popen)(cmd, **kwargs)
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            state.update(
                {
                    "turn_status": "errored",
                    "process_status": "exited",
                    "last_error": error,
                    "updated_at": now_iso(),
                }
            )
            atomic_write_json(state_path, state)
            event = {
                "type": "agent_error",
                "task_name": task_name,
                "parent_session_id": parent_session_id,
                "permission_profile": permission_metadata["permission_profile"],
                "error": error,
                "agent_type": agent_type,
            }
            append_jsonl_event(task_dir / "events.jsonl", event)
            append_parent_inbox_event(task_dir, event)
            bus_event = self.event_bus.append_event(
                "agent_error",
                agent_path=state.get("agent_path"),
                run_id=state.get("run_id"),
                task_name=task_name,
                status={"turn_status": state.get("turn_status"), "process_status": state.get("process_status")},
                payload={"error": error, "agent_type": agent_type},
                notify=True,
            )
            state["last_event_seq"] = bus_event["event_seq"]
            atomic_write_json(state_path, state)
            self._write_registry_entry(task_name, state, task_dir)
            raise
        finally:
            stdout.close()
            stderr.close()
        pid = getattr(proc, "pid", None)
        state.update({"pid": pid, "process_status": "alive", "updated_at": now_iso()})
        atomic_write_json(state_path, state)
        event = {
            "type": "agent_started",
            "task_name": task_name,
            "parent_session_id": parent_session_id,
            "pid": pid,
            "permission_profile": permission_metadata["permission_profile"],
            "agent_type": agent_type,
        }
        append_jsonl_event(task_dir / "events.jsonl", event)
        append_parent_inbox_event(task_dir, event)
        bus_event = self.event_bus.append_event(
            "agent_started",
            agent_path=state.get("agent_path"),
            run_id=state.get("run_id"),
            task_name=task_name,
            status={"turn_status": state.get("turn_status"), "process_status": state.get("process_status")},
            payload={"pid": pid, "permission_profile": permission_metadata["permission_profile"], "agent_type": agent_type},
        )
        state["last_event_seq"] = bus_event["event_seq"]
        atomic_write_json(state_path, state)
        self._write_registry_entry(task_name, state, task_dir)
        return AgentHandle(
            task_name,
            state["agent_path"],
            pid,
            str(task_dir),
            str(state_path),
            cmd,
            run_id=state.get("run_id"),
            artifact_dir=state.get("artifact_dir") or str(task_dir),
            permission_profile=state.get("permission_profile"),
            parent_permission_mode=state.get("parent_permission_mode"),
            permission_options=state.get("permission_options"),
            agent_type=state.get("agent_type"),
            role_source_path=state.get("role_source_path"),
            background=state.get("background", True),
            ipc_mode=state.get("ipc_mode"),
            effective_ipc_mode=state.get("effective_ipc_mode"),
            ipc_fallback_reason=state.get("ipc_fallback_reason"),
            isolation=state.get("isolation"),
            worktree_path=state.get("worktree_path"),
            ipc_endpoint=state.get("ipc_endpoint"),
        )

    def register_agent(self, task_name, state, task_dir=None):
        task_name = self._task_name_from_target(task_name)
        task_dir = Path(task_dir) if task_dir is not None else self.temp_dir / task_name
        task_dir.mkdir(parents=True, exist_ok=True)
        self._write_registry_entry(task_name, state, task_dir)
        return self._registry_entry(task_name, state, task_dir)

    def list_agents(self, path_prefix=None, include_closed=False):
        registry_entries = self.registry.list_agents(path_prefix=path_prefix, include_closed=True)
        if registry_entries:
            states = []
            for entry in registry_entries:
                if not include_closed and entry.status == "closed":
                    continue
                try:
                    state = self.read_agent(entry.agent_path)
                except (FileNotFoundError, ValueError):
                    continue
                if not include_closed and state.process_status in {"shutdown", "killed"}:
                    continue
                states.append(state)
            return states
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
        return self._queue_message(target, message, author=author, trigger_turn=True)

    def resume_agent(self, target, message, *, author="/root"):
        previous = self.read_agent(target)
        if not previous.parent_session_id or not previous.run_id:
            raise ValueError("parent_session_id and run_id are required to resume a subagent")
        task_dir = Path(previous.task_dir)
        state_path = task_dir / "state.json"
        raw = read_json_or_none(state_path) or {}
        resume_context = SubagentTranscriptStore(self.temp_dir / "sessions").build_resume_context(previous.parent_session_id, previous.run_id)
        for stale_name in ("_stop", "reply.txt"):
            try:
                (task_dir / stale_name).unlink()
            except FileNotFoundError:
                pass
        atomic_write_json(task_dir / "_history.json", resume_context.get("backend_history") or [])
        (task_dir / "input.txt").write_text(message, encoding="utf-8")
        next_round = int(raw.get("round") if raw.get("round") is not None else previous.round) + 1
        output_path = task_dir / f"output{next_round}.txt"
        llm_no = int(raw.get("llm_no") if raw.get("llm_no") is not None else 0)
        verbose = bool(raw.get("verbose", False))
        permission_options = raw.get("permission_options") or previous.permission_options or {}
        permission_profile = raw.get("permission_profile") or previous.permission_profile or INHERIT_CURRENT_PERMISSIONS
        parent_permission_mode = raw.get("parent_permission_mode") if raw.get("parent_permission_mode") is not None else previous.parent_permission_mode
        isolation = raw.get("isolation") or previous.isolation
        worktree_path = raw.get("worktree_path") or previous.worktree_path
        agentmain_path = Path(worktree_path) / "agentmain.py" if isolation == "worktree" and worktree_path else self.repo_dir / "agentmain.py"
        cmd = [
            self.python_executable,
            str(agentmain_path),
            "--task",
            previous.task_name,
            "--nobg",
            "--task_root",
            str(self.root_dir),
            "--llm_no",
            str(llm_no),
            "--permission_profile",
            permission_profile,
            "--parent_permission_mode",
            parent_permission_mode or "",
            "--permission_options",
            json.dumps(permission_options, ensure_ascii=False, sort_keys=True),
        ]
        if verbose:
            cmd.append("--verbose")
        stdout = open(task_dir / f"stdout{next_round}.log", "w", encoding="utf-8")
        stderr = open(task_dir / f"stderr{next_round}.log", "w", encoding="utf-8")
        try:
            kwargs = self._child_popen_kwargs(worktree_path, stdout, stderr, previous.agent_path)
            proc = (self.popen or __import__("subprocess").Popen)(cmd, **kwargs)
        except Exception as e:
            error = f"{type(e).__name__}: {e}"
            raw.update(
                {
                    "schema_version": 1,
                    "task_name": previous.task_name,
                    "agent_path": previous.agent_path,
                    "turn_status": "errored",
                    "process_status": "exited",
                    "last_error": error,
                    "last_message": message,
                    "updated_at": now_iso(),
                }
            )
            atomic_write_json(state_path, raw)
            append_jsonl_event(task_dir / "events.jsonl", {"type": "agent_error", "task_name": previous.task_name, "run_id": previous.run_id, "error": error})
            self._write_registry_entry(previous.task_name, raw, task_dir)
            raise
        finally:
            stdout.close()
            stderr.close()
        pid = getattr(proc, "pid", None)
        raw.update(
            {
                "schema_version": 1,
                "task_name": previous.task_name,
                "agent_path": previous.agent_path,
                "parent_session_id": previous.parent_session_id,
                "run_id": previous.run_id,
                "artifact_dir": previous.artifact_dir or raw.get("artifact_dir") or str(task_dir),
                "permission_profile": permission_profile,
                "parent_permission_mode": parent_permission_mode,
                "permission_options": permission_options,
                "agent_type": raw.get("agent_type") or previous.agent_type,
                "role_source_path": raw.get("role_source_path") or previous.role_source_path,
                "background": raw.get("background", previous.background),
                "handoff_mode": raw.get("handoff_mode") or previous.handoff_mode,
                "handoff_reason": raw.get("handoff_reason") or previous.handoff_reason,
                "ipc_mode": raw.get("ipc_mode") or previous.ipc_mode,
                "effective_ipc_mode": raw.get("effective_ipc_mode") or previous.effective_ipc_mode,
                "ipc_fallback_reason": raw.get("ipc_fallback_reason") if raw.get("ipc_fallback_reason") is not None else previous.ipc_fallback_reason,
                "isolation": isolation,
                "worktree_path": worktree_path,
                "pid": pid,
                "round": next_round,
                "turn_status": "pending",
                "process_status": "alive",
                "input_path": str(task_dir / "input.txt"),
                "output_path": str(output_path),
                "final_output_path": previous.final_output_path,
                "final_output_ref": None,
                "final_output_sha256": None,
                "last_message": message,
                "last_error": None,
                "resume_source": "sidechain_transcript",
                "resume_context_event_count": resume_context.get("source_event_count"),
                "llm_no": llm_no,
                "verbose": verbose,
                "updated_at": now_iso(),
            }
        )
        atomic_write_json(state_path, raw)
        local_event = {
            "type": "agent_resumed",
            "task_name": previous.task_name,
            "agent_path": previous.agent_path,
            "run_id": previous.run_id,
            "pid": pid,
            "author": author,
            "resume_source": "sidechain_transcript",
            "resume_context_event_count": resume_context.get("source_event_count"),
        }
        append_jsonl_event(task_dir / "events.jsonl", local_event)
        append_parent_inbox_event(task_dir, local_event)
        bus_event = self.event_bus.append_event(
            "agent_resumed",
            agent_path=previous.agent_path,
            run_id=previous.run_id,
            task_name=previous.task_name,
            status={"turn_status": raw.get("turn_status"), "process_status": raw.get("process_status")},
            payload={"pid": pid, "resume_source": "sidechain_transcript", "resume_context_event_count": resume_context.get("source_event_count")},
            notify=True,
        )
        raw["last_event_seq"] = bus_event["event_seq"]
        atomic_write_json(state_path, raw)
        self._write_registry_entry(previous.task_name, raw, task_dir)
        self.registry.mark_running(previous.agent_path, pid=pid, turn_status="pending", process_status="alive")
        handle = AgentHandle(
            previous.task_name,
            previous.agent_path,
            pid,
            str(task_dir),
            str(state_path),
            cmd,
            run_id=previous.run_id,
            artifact_dir=raw.get("artifact_dir") or str(task_dir),
            permission_profile=permission_profile,
            parent_permission_mode=parent_permission_mode,
            permission_options=permission_options,
            agent_type=raw.get("agent_type"),
            role_source_path=raw.get("role_source_path"),
            background=raw.get("background", True),
            ipc_mode=raw.get("ipc_mode"),
            effective_ipc_mode=raw.get("effective_ipc_mode"),
            ipc_fallback_reason=raw.get("ipc_fallback_reason"),
            isolation=isolation,
            worktree_path=worktree_path,
            handoff_mode=raw.get("handoff_mode"),
            handoff_reason=raw.get("handoff_reason"),
        )
        return ResumeResult(previous.agent_path, previous, handle, resume_context)

    def wait_agents(self, targets=None, timeout_s=30, poll_interval_s=0.5, since_event_offsets=None, since_event_seq=None):
        deadline = time.monotonic() + timeout_s
        if targets is None:
            targets = [state.task_name for state in self.list_agents()]
        else:
            targets = [self._task_name_from_target(target) for target in targets if str(target).strip()]
        if not targets:
            return WaitResult(True, [], "No subagents to wait for.", next_event_seq=self.event_bus.last_event_seq())
        if since_event_seq is not None:
            bus_events = self.event_bus.read_events_since(since_event_seq, targets=targets)
            if bus_events:
                next_event_seq = int(bus_events[-1].get("event_seq") or self.event_bus.last_event_seq())
                return WaitResult(
                    False,
                    self._states_for_events(bus_events, targets),
                    "Subagent event update received.",
                    bus_events,
                    next_event_seq,
                )
            if timeout_s <= 0:
                return WaitResult(True, [], "Wait timed out.", [], self.event_bus.last_event_seq())
        baseline = since_event_offsets or {target: self._event_size(target) for target in targets}
        inbox_baseline = self._parent_inbox_size()
        while True:
            if since_event_seq is not None:
                bus_events = self.event_bus.read_events_since(since_event_seq, targets=targets)
                if bus_events:
                    next_event_seq = int(bus_events[-1].get("event_seq") or self.event_bus.last_event_seq())
                    return WaitResult(
                        False,
                        self._states_for_events(bus_events, targets),
                        "Subagent event update received.",
                        bus_events,
                        next_event_seq,
                    )
            inbox_events = self._read_parent_inbox_events_since(inbox_baseline, targets)
            if inbox_events:
                return WaitResult(
                    False,
                    self._states_for_events(inbox_events, targets),
                    "Subagent mailbox update received.",
                    inbox_events,
                    self.event_bus.last_event_seq(),
                )
            changed = []
            state_events = []
            for target in targets:
                # probe_agent, not read_agent: detection runs every poll interval and must not
                # write. Once something is worth reporting, _states_for_events re-reads through
                # read_agent so the returned state is still the persisted one.
                state = self.probe_agent(target)
                if self._is_notify_state(state):
                    changed.append(state)
                    state_events.append({"type": "state_notify", "task_name": state.task_name, "agent_path": state.agent_path})
                elif self._event_size(target) != baseline.get(target, 0):
                    changed.append(state)
                    state_events.append({"type": "task_event_file_changed", "task_name": state.task_name, "agent_path": state.agent_path})
            if changed:
                return WaitResult(
                    False,
                    self._states_for_events(state_events, targets),
                    "Subagent state update received.",
                    state_events,
                    self.event_bus.last_event_seq(),
                )
            if time.monotonic() >= deadline:
                return WaitResult(True, [], "Wait timed out.", [], self.event_bus.last_event_seq())
            self._wait_for_change(targets, poll_interval_s, deadline)

    def _wait_for_change(self, targets, poll_interval_s, deadline):
        """Sleep until something might have changed: on the channels if there are any.

        With live channels this is the watch half of B3 — the child signals over the same
        connection it already subscribes on, so a turn_completed no longer costs a poll
        interval. Without them (ipc_mode=file, or realtime refused) the blind sleep has to
        stay, because the file transport has nothing to be woken by.
        """
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False
        budget = min(float(poll_interval_s), remaining)
        channels = self._channels_for(targets)
        if not channels:
            self.sleep(poll_interval_s)
            return False
        # One thread cannot block on several channels at once, so the budget is split: each
        # channel gets a slice, and any signal ends the wait immediately.
        slice_s = max(budget / len(channels), 0.0)
        woken = False
        for channel in channels:
            try:
                if channel.wait_for_signal(slice_s):
                    woken = True
                    break
            except Exception:
                # A dead channel degrades to polling rather than failing the wait.
                self.sleep(slice_s)
        return woken

    def _channels_for(self, targets):
        """_realtime_channels is keyed by agent_path; wait_agents works in task names."""
        if not self._realtime_channels:
            return []
        names = {self._task_name_from_target(target) for target in targets}
        channels = []
        for agent_path, channel in self._realtime_channels.items():
            if channel is None:
                continue
            if str(agent_path).rstrip("/").split("/")[-1] in names:
                channels.append(channel)
        return channels

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

    def request_foreground(self, target, reason="parent_foreground"):
        return self._request_handoff(target, background=False, handoff_mode="foreground", event_type="foreground_requested", reason=reason)

    def request_background(self, target, reason="parent_background"):
        return self._request_handoff(target, background=True, handoff_mode="background", event_type="background_requested", reason=reason)

    def attach_agent(self, target, *, since_offset=0, max_chars=None, reason="parent_attach"):
        return self._handoff_with_stream(
            target,
            background=False,
            handoff_mode="foreground",
            event_type="foreground_requested",
            reason=reason,
            attach_status="attached",
            since_offset=since_offset,
            max_chars=max_chars,
        )

    def detach_agent(self, target, *, since_offset=0, max_chars=None, reason="parent_detach"):
        return self._handoff_with_stream(
            target,
            background=True,
            handoff_mode="background",
            event_type="background_requested",
            reason=reason,
            attach_status="detached",
            since_offset=since_offset,
            max_chars=max_chars,
        )

    def _handoff_with_stream(self, target, *, background, handoff_mode, event_type, reason, attach_status, since_offset, max_chars):
        handoff = self._request_handoff(
            target,
            background=background,
            handoff_mode=handoff_mode,
            event_type=event_type,
            reason=reason,
            attach_status=attach_status,
        )
        state = handoff.updated_state
        raw = read_json_or_none(Path(state.task_dir) / "state.json") or {}
        last_event_seq = raw.get("last_event_seq")
        slice_info = self._read_output_slice(state.output_path, since_offset=since_offset, max_chars=max_chars)
        return AttachResult(
            target=handoff.target,
            state=state,
            handoff_mode=handoff_mode,
            attach_status=attach_status,
            reason=reason,
            output_path=state.output_path,
            stream_text=slice_info["text"],
            stream_offset=slice_info["offset"],
            next_stream_offset=slice_info["next_offset"],
            stream_truncated=slice_info["truncated"],
            stream_eof=slice_info["eof"],
            next_event_seq=int(last_event_seq) + 1 if last_event_seq is not None else None,
        )

    def _read_output_slice(self, output_path, *, since_offset=0, max_chars=None):
        offset = max(0, int(since_offset or 0))
        full_text = ""
        if output_path:
            try:
                full_text = Path(output_path).read_text(encoding="utf-8", errors="replace")
            except OSError:
                full_text = ""
        chunk = full_text[offset:]
        truncated = False
        if max_chars is not None and int(max_chars) >= 0 and len(chunk) > int(max_chars):
            chunk = chunk[: int(max_chars)]
            truncated = True
        return {
            "text": chunk,
            "offset": offset,
            "next_offset": offset + len(chunk),
            "truncated": truncated,
            "eof": (not truncated) and ROUND_END_MARKER in full_text,
        }

    def _request_handoff(self, target, *, background, handoff_mode, event_type, reason, attach_status=None):
        previous = self.read_agent(target)
        task_dir = Path(previous.task_dir)
        raw = read_json_or_none(task_dir / "state.json") or {}
        raw.update(
            {
                "schema_version": 1,
                "task_name": previous.task_name,
                "agent_path": previous.agent_path,
                "background": bool(background),
                "handoff_mode": handoff_mode,
                "handoff_reason": reason,
                "updated_at": now_iso(),
            }
        )
        if attach_status is not None:
            raw["attach_status"] = attach_status
        atomic_write_json(task_dir / "state.json", raw)
        local_event = {
            "type": event_type,
            "task_name": previous.task_name,
            "agent_path": previous.agent_path,
            "run_id": previous.run_id,
            "reason": reason,
            "background": bool(background),
            "handoff_mode": handoff_mode,
        }
        if attach_status is not None:
            local_event["attach_status"] = attach_status
        append_jsonl_event(task_dir / "events.jsonl", local_event)
        payload = {"reason": reason, "background": bool(background), "handoff_mode": handoff_mode}
        if attach_status is not None:
            payload["attach_status"] = attach_status
        bus_event = self.event_bus.append_event(
            event_type,
            agent_path=previous.agent_path,
            run_id=previous.run_id,
            task_name=previous.task_name,
            status={"turn_status": previous.turn_status, "process_status": previous.process_status},
            payload=payload,
            notify=True,
        )
        raw["last_event_seq"] = bus_event["event_seq"]
        atomic_write_json(task_dir / "state.json", raw)
        updated = self._agent_state_from_dict(previous.task_name, task_dir, raw)
        self._write_registry_entry(previous.task_name, raw, task_dir)
        return HandoffResult(previous.agent_path, previous, updated, handoff_mode, reason)

    def close_agent(self, target, reason="parent_cleanup", grace_s=2.0, cleanup_worktree=False, cascade=False):
        """Close ``target``; with ``cascade=True`` close everything below it first.

        Without cascade, closing a middle-tier agent orphans its children: they keep running
        with nobody reading their output and keep consuming G1 active-agent slots until their
        process happens to die. Descendants go deepest-first so a child is always gone before
        the parent that spawned it, and the target itself is closed last — a cascade that dies
        halfway must not leave the target alive as well, so per-descendant failures are
        recorded and the sweep continues.
        """
        if str(target or "").strip().replace("\\", "/").rstrip("/") in {"root", "/root"}:
            raise ValueError("cannot close root agent")
        closed_descendants = []
        if cascade:
            closed_descendants = self._close_descendants(
                target, grace_s=grace_s, cleanup_worktree=cleanup_worktree
            )
        result = self._close_single_agent(
            target, reason=reason, grace_s=grace_s, cleanup_worktree=cleanup_worktree
        )
        result.closed_descendants = closed_descendants
        return result

    def _close_descendants(self, target, *, grace_s, cleanup_worktree):
        """Close every live agent below ``target``, deepest first; never raise."""
        try:
            anchor = self.read_agent(target).agent_path
        except Exception:
            anchor = str(target)
        try:
            entries = self.registry.descendants(anchor)
        except Exception:
            return []
        cascade_reason = f"cascade_close:{anchor}"
        rows = []
        for entry in entries:
            row = {"agent_path": str(entry.agent_path), "task_name": entry.task_name, "reason": cascade_reason}
            try:
                closed = self._close_single_agent(
                    str(entry.agent_path),
                    reason=cascade_reason,
                    grace_s=grace_s,
                    cleanup_worktree=cleanup_worktree,
                )
                row.update(
                    {
                        "status": "closed",
                        "previous_turn_status": closed.previous_state.turn_status,
                        "closed_process_status": closed.closed_state.process_status,
                        "final_output_path": closed.final_output_path,
                    }
                )
            except Exception as exc:
                # One stuck descendant must not abort the sweep: aborting would leave both the
                # remaining descendants and the target itself running.
                row.update({"status": "error", "msg": f"{type(exc).__name__}: {exc}"})
            rows.append(row)
        return rows

    def _close_single_agent(self, target, reason="parent_cleanup", grace_s=2.0, cleanup_worktree=False):
        if str(target or "").strip().replace("\\", "/").rstrip("/") in {"root", "/root"}:
            raise ValueError("cannot close root agent")
        previous = self.read_agent(target)
        task_dir = Path(previous.task_dir)
        (task_dir / "_stop").write_text(reason, encoding="utf-8")
        close_requested = self.event_bus.append_event(
            "close_requested",
            agent_path=previous.agent_path,
            run_id=previous.run_id,
            task_name=previous.task_name,
            status={"turn_status": previous.turn_status, "process_status": previous.process_status},
            payload={"reason": reason},
        )
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
        if raw.get("isolation") == "worktree" and raw.get("worktree_path"):
            raw["worktree_summary"] = self._summarize_worktree(raw.get("worktree_path"))
            if cleanup_worktree:
                from subagent_worktree import remove_subagent_worktree

                raw["worktree_cleanup"] = remove_subagent_worktree(self.root_dir, raw.get("worktree_path"), runner=self.worktree_runner)
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
        append_parent_inbox_event(
            task_dir,
            {
                "type": "agent_closed",
                "task_name": previous.task_name,
                "previous_turn_status": previous.turn_status,
                "previous_process_status": previous.process_status,
                "closed_process_status": close_process_status,
                "reason": reason,
            },
        )
        if raw.get("parent_session_id") and raw.get("run_id"):
            try:
                SubagentTranscriptStore(self.temp_dir / "sessions").append_event(
                    raw.get("parent_session_id"),
                    raw.get("run_id"),
                    "agent_closed",
                    {
                        "task_name": previous.task_name,
                        "agent_path": previous.agent_path,
                        "previous_turn_status": previous.turn_status,
                        "previous_process_status": previous.process_status,
                        "closed_process_status": close_process_status,
                        "reason": reason,
                    },
                )
            except Exception:
                pass
        closed = self._agent_state_from_dict(previous.task_name, task_dir, raw)
        closed_event = self.event_bus.append_event(
            "agent_closed",
            agent_path=previous.agent_path,
            run_id=previous.run_id,
            task_name=previous.task_name,
            status={"turn_status": raw.get("turn_status"), "process_status": raw.get("process_status")},
            payload={"reason": reason, "previous_turn_status": previous.turn_status, "previous_process_status": previous.process_status},
            notify=True,
        )
        raw["last_event_seq"] = closed_event["event_seq"]
        atomic_write_json(task_dir / "state.json", raw)
        self.registry.mark_closed(previous.agent_path, previous_status=previous.turn_status, closed_status=close_process_status)
        self._close_realtime_channel(previous.agent_path, task_dir=task_dir)
        return CloseResult(target, previous, closed, previous.final_output_path)

    def _queue_message(self, target, message, *, author, trigger_turn):
        task_name = self._task_name_from_target(target)
        task_dir = self.temp_dir / task_name
        task_dir.mkdir(parents=True, exist_ok=True)
        delivery_mode = TRIGGER_TURN if trigger_turn else QUEUE_ONLY
        row = SubagentMailbox(task_dir / "mailbox.jsonl").enqueue(
            message,
            author=author,
            recipient=f"/root/{task_name}",
            delivery_mode=delivery_mode,
            source_tool="followup_task" if trigger_turn else "send_message",
        )
        event = {
            "type": "message_queued",
            "task_name": task_name,
            "author": author,
            "trigger_turn": trigger_turn,
            "delivery_mode": delivery_mode,
            "message_id": row.get("message_id"),
        }
        append_jsonl_event(task_dir / "events.jsonl", event)
        bus_event = self.event_bus.append_event(
            "message_queued",
            agent_path=f"/root/{task_name}",
            task_name=task_name,
            payload=event,
        )
        row["event_seq"] = bus_event["event_seq"]
        return row

    def _write_registry_entry(self, task_name, state, task_dir):
        agent_path = state.get("agent_path") or f"/root/{task_name}"
        try:
            self.registry.update(
                agent_path,
                pid=state.get("pid"),
                task_dir=str(task_dir),
                state_path=str(task_dir / "state.json"),
                artifact_dir=state.get("artifact_dir") or str(task_dir),
                parent_session_id=state.get("parent_session_id"),
                last_task_message=state.get("last_message"),
                turn_status=state.get("turn_status"),
                process_status=state.get("process_status"),
                parent_permission_mode=state.get("parent_permission_mode"),
                permission_profile=state.get("permission_profile"),
                permission_options=state.get("permission_options"),
                agent_type=state.get("agent_type"),
                role_source_path=state.get("role_source_path"),
                background=state.get("background", True),
                ipc_mode=state.get("ipc_mode"),
                effective_ipc_mode=state.get("effective_ipc_mode"),
                ipc_fallback_reason=state.get("ipc_fallback_reason"),
                isolation=state.get("isolation"),
                worktree_path=state.get("worktree_path"),
            )
        except FileNotFoundError:
            parent_path = AgentPath.parse(agent_path).parent or AgentPath.root()
            self.registry.create_child(
                parent_path=parent_path,
                task_name=task_name,
                task_dir=task_dir,
                state_path=task_dir / "state.json",
                pid=state.get("pid"),
                parent_session_id=state.get("parent_session_id"),
                last_task_message=state.get("last_message"),
                parent_permission_mode=state.get("parent_permission_mode"),
                permission_profile=state.get("permission_profile"),
                permission_options=state.get("permission_options"),
                agent_type=state.get("agent_type"),
                role_source_path=state.get("role_source_path"),
                background=state.get("background", True),
                ipc_mode=state.get("ipc_mode"),
                effective_ipc_mode=state.get("effective_ipc_mode"),
                ipc_fallback_reason=state.get("ipc_fallback_reason"),
                isolation=state.get("isolation"),
                worktree_path=state.get("worktree_path"),
            )

    def _registry_entry(self, task_name, state, task_dir):
        return {
            "task_name": task_name,
            "agent_path": state.get("agent_path") or f"/root/{task_name}",
            "parent_path": "/root",
            "parent_session_id": state.get("parent_session_id"),
            "run_id": state.get("run_id"),
            "artifact_dir": state.get("artifact_dir") or str(task_dir),
            "pid": state.get("pid"),
            "task_dir": str(task_dir),
            "state_path": str(task_dir / "state.json"),
            "last_task_message": state.get("last_message"),
            "turn_status": state.get("turn_status"),
            "process_status": state.get("process_status"),
            "permission_profile": state.get("permission_profile"),
            "parent_permission_mode": state.get("parent_permission_mode"),
            "permission_options": state.get("permission_options") or {},
            "agent_type": state.get("agent_type"),
            "role_source_path": state.get("role_source_path"),
            "background": state.get("background", True),
            "ipc_mode": state.get("ipc_mode"),
            "effective_ipc_mode": state.get("effective_ipc_mode"),
            "ipc_fallback_reason": state.get("ipc_fallback_reason"),
            "isolation": state.get("isolation"),
            "worktree_path": state.get("worktree_path"),
            "created_at": state.get("started_at"),
            "closed_at": None,
        }

    def _next_available_task_name(self, task_name):
        base = self._task_name_from_target(task_name)
        index = 0
        while True:
            candidate = base if index == 0 else f"{base}_{index}"
            if not (self.temp_dir / candidate / "state.json").exists():
                try:
                    self.registry.get(f"/root/{candidate}")
                except FileNotFoundError:
                    return candidate
            index += 1

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

    def _fork_metadata(self, fork_mode, history_to_write):
        history = list(history_to_write or [])
        estimate = sum(len(json.dumps(item, ensure_ascii=False, default=str)) for item in history) // 4
        warning = None
        if fork_mode == "all" and history:
            warning = "full history fork may include unrelated context"
        return {
            "fork_history_count": len(history),
            "fork_history_token_estimate": estimate,
            "fork_redacted": False,
            "fork_policy_warning": warning,
        }

    def _event_size(self, target):
        task_name = self._task_name_from_target(target)
        path = self.temp_dir / task_name / "events.jsonl"
        try:
            return path.stat().st_size
        except FileNotFoundError:
            return 0

    def _parent_inbox_path(self):
        return self.temp_dir / "subagents" / "inbox.jsonl"

    def _parent_inbox_size(self):
        try:
            return self._parent_inbox_path().stat().st_size
        except FileNotFoundError:
            return 0

    def _read_parent_inbox_events_since(self, offset, targets):
        path = self._parent_inbox_path()
        try:
            size = path.stat().st_size
        except FileNotFoundError:
            return []
        offset = int(offset or 0)
        if offset < 0 or offset > size:
            offset = 0
        if size <= offset:
            return []
        try:
            with open(path, "rb") as f:
                f.seek(offset)
                raw = f.read().decode("utf-8", errors="replace")
        except OSError:
            return []
        target_set = set(targets or [])
        events = []
        for line in raw.splitlines():
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                continue
            task_name = row.get("task_name")
            if not task_name and row.get("agent_path"):
                task_name = str(row.get("agent_path")).rstrip("/").split("/")[-1]
            try:
                task_name = self._task_name_from_target(task_name)
            except (TypeError, ValueError):
                continue
            if target_set and task_name not in target_set:
                continue
            row["task_name"] = task_name
            row.setdefault("agent_path", f"/root/{task_name}")
            events.append(row)
        return events

    def _states_for_events(self, events, fallback_targets):
        names = []
        for event in events:
            task_name = event.get("task_name")
            if task_name and task_name not in names:
                names.append(task_name)
        if not names:
            names = list(fallback_targets or [])
        states = []
        for name in names:
            try:
                states.append(self.read_agent(name))
            except Exception:
                continue
        return states

    def _is_notify_state(self, state):
        return state.turn_status in {"completed", "errored", "interrupted"} or state.process_status in {
            "waiting_reply",
            "exited",
            "shutdown",
            "killed",
        }

    def _refresh_state(self, task_name, task_dir, raw, *, persist_side_effects=True):
        if persist_side_effects:
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
        artifact = self._final_output_artifact(raw)
        if artifact is not None:
            artifact_path = Path(artifact.get("path") or "")
            if artifact_path.exists():
                turn_status = "completed"
                process_status = raw.get("process_status") or "exited"
                final_output_path = str(artifact_path)
                final_output_sha256 = artifact.get("sha256")
                has_round_end = True
                output_path = artifact_path

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
                "agent_type": raw.get("agent_type"),
                "role_source_path": raw.get("role_source_path"),
                "background": raw.get("background", True),
                "ipc_mode": raw.get("ipc_mode"),
                "effective_ipc_mode": raw.get("effective_ipc_mode"),
                "ipc_fallback_reason": raw.get("ipc_fallback_reason"),
                "ipc_endpoint": raw.get("ipc_endpoint"),
                "isolation": raw.get("isolation"),
                "worktree_path": raw.get("worktree_path"),
                "worktree_summary": raw.get("worktree_summary"),
                "worktree_cleanup": raw.get("worktree_cleanup"),
                "updated_at": now_iso(),
            }
        )
        if (
            persist_side_effects
            and refreshed.get("isolation") == "worktree"
            and refreshed.get("worktree_path")
            and not refreshed.get("worktree_cleanup")
        ):
            # Shelling out to git is exactly the kind of work a poll loop must not repeat.
            refreshed["worktree_summary"] = self._summarize_worktree(refreshed.get("worktree_path"))
        return refreshed

    def _final_output_artifact(self, raw):
        artifact_ref = raw.get("final_output_ref")
        artifact_dir = raw.get("artifact_dir")
        if not artifact_ref or not artifact_dir:
            return None
        try:
            return SubagentArtifactStore(artifact_dir).get(artifact_ref)
        except Exception:
            return None

    def _summarize_worktree(self, worktree_path):
        try:
            from subagent_worktree import summarize_subagent_worktree

            return summarize_subagent_worktree(worktree_path, runner=self.worktree_runner)
        except Exception as e:
            return {"status": "error", "worktree_path": str(worktree_path), "changed_files": [], "status_text": "", "diff": "", "error": f"{type(e).__name__}: {e}"}

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
            parent_session_id=raw.get("parent_session_id"),
            run_id=raw.get("run_id"),
            artifact_dir=raw.get("artifact_dir") or str(task_dir),
            permission_profile=raw.get("permission_profile"),
            parent_permission_mode=raw.get("parent_permission_mode"),
            permission_options=raw.get("permission_options") or {},
            agent_type=raw.get("agent_type"),
            role_source_path=raw.get("role_source_path"),
            background=bool(raw.get("background", True)),
            ipc_mode=raw.get("ipc_mode"),
            effective_ipc_mode=raw.get("effective_ipc_mode"),
            ipc_fallback_reason=raw.get("ipc_fallback_reason"),
            isolation=raw.get("isolation"),
            worktree_path=raw.get("worktree_path"),
            handoff_mode=raw.get("handoff_mode"),
            handoff_reason=raw.get("handoff_reason"),
            worktree_summary=raw.get("worktree_summary"),
            worktree_cleanup=raw.get("worktree_cleanup"),
            attach_status=raw.get("attach_status"),
            ipc_endpoint=raw.get("ipc_endpoint"),
            close_reason=raw.get("close_reason"),
        )


_DEFAULT_MANAGER = SubagentManager()


def read_agent(target):
    return _DEFAULT_MANAGER.read_agent(target)


def list_agents(path_prefix=None, include_closed=False):
    return _DEFAULT_MANAGER.list_agents(path_prefix, include_closed=include_closed)


def register_agent(task_name, state, task_dir=None):
    return _DEFAULT_MANAGER.register_agent(task_name, state, task_dir)


def wait_agents(targets=None, timeout_s=30, poll_interval_s=0.5, since_event_offsets=None, since_event_seq=None):
    return _DEFAULT_MANAGER.wait_agents(targets, timeout_s, poll_interval_s, since_event_offsets, since_event_seq)


def close_agent(target, reason="parent_cleanup", grace_s=2.0, cascade=False):
    return _DEFAULT_MANAGER.close_agent(target, reason=reason, grace_s=grace_s, cascade=cascade)


def interrupt_agent(target, reason="parent_interrupt"):
    return _DEFAULT_MANAGER.interrupt_agent(target, reason=reason)


def spawn_agent(
    task_name,
    message,
    *,
    llm_no=0,
    verbose=False,
    parent_session_id=None,
    fork_turns="none",
    fork_history=None,
    permission_profile=INHERIT_CURRENT_PERMISSIONS,
    parent_permission_mode=None,
    permission_options=None,
):
    return _DEFAULT_MANAGER.spawn_agent(
        task_name,
        message,
        llm_no=llm_no,
        verbose=verbose,
        parent_session_id=parent_session_id,
        fork_turns=fork_turns,
        fork_history=fork_history,
        permission_profile=permission_profile,
        parent_permission_mode=parent_permission_mode,
        permission_options=permission_options,
    )


def send_message(target, message, *, author="/root"):
    return _DEFAULT_MANAGER.send_message(target, message, author=author)


def followup_task(target, message, *, author="/root"):
    return _DEFAULT_MANAGER.followup_task(target, message, author=author)


def resume_agent(target, message, *, author="/root"):
    return _DEFAULT_MANAGER.resume_agent(target, message, author=author)
