from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from subagent_agent_path import AgentPath
from subagent_state import atomic_write_json, cross_process_lock, now_iso, read_json_or_none


DEFAULT_MAX_DEPTH = 3
DEFAULT_MAX_ACTIVE_AGENTS = 32
# A row is registered before Popen, so ``pid is None`` legitimately means "starting".
# Past this window it means the launch never happened, and the slot must come back.
DEFAULT_STARTUP_GRACE_S = 120.0
# A child that says any of these about itself cannot come back; its slot is reusable even
# if a recycled pid is currently alive, because that pid is a different process.
TERMINAL_PROCESS_STATUSES = frozenset({"exited", "shutdown", "killed", "errored", "failed"})


class SubagentTreeLimitError(RuntimeError):
    """Raised when a spawn would exceed the agent tree depth or active-agent cap.

    Every GA subagent is a separate OS process and can spawn its own children, so an
    unbounded tree burns processes, memory and real LLM spend. Codex guards the same thing
    with AgentRegistry { active_agents, total_count } and reserve_spawn_slot.
    """

    def __init__(self, message, *, reason="unknown"):
        super().__init__(message)
        self.reason = reason


class SubagentNameConflictError(RuntimeError):
    """Raised when a spawn would land on the name of an agent that is still live.

    GA used to silently rename ``reviewer`` to ``reviewer_1``, so asking for an agent that
    already exists produced a second OS process, a second active-agent slot and a second real
    LLM spend while the caller believed there was one agent. Codex refuses the same situation
    outright (`codex-rs/core/src/agent/registry.rs:247-250`). The rename is still correct once
    the previous agent is gone — there it protects that agent's artifacts from being
    overwritten — so only live names conflict.

    Carries ``agent_path`` so the tool layer can hand the model the conflicting path as data
    instead of making it re-parse the sentence.
    """

    def __init__(self, message, agent_path=None):
        super().__init__(message)
        self.agent_path = agent_path


def resolve_tree_limits_from_env(env=None):
    """Read the tree caps from the environment; unset or non-positive values fall through."""
    if env is None:
        import os

        env = os.environ
    limits = {}
    for key, name in (("GA_SUBAGENT_MAX_DEPTH", "max_depth"), ("GA_SUBAGENT_MAX_ACTIVE", "max_active_agents")):
        raw = env.get(key)
        if raw is None or not str(raw).strip():
            continue
        try:
            value = int(str(raw).strip())
        except (TypeError, ValueError):
            continue
        if value <= 0:
            continue
        limits[name] = value
    return limits


def _default_process_exists(pid):
    if not pid:
        return False
    try:
        import psutil

        return psutil.pid_exists(int(pid))
    except Exception:
        try:
            import os

            os.kill(int(pid), 0)
            return True
        except OSError:
            return False


def _default_process_identity(pid):
    """Return the OS process start time, or ``None`` when it cannot be determined.

    Windows recycles pids, so ``pid_exists`` alone let an unrelated process masquerade as a
    finished subagent and hold its slot forever. Start time is the identity that separates
    "the same process" from "a different process that inherited the number".
    """
    if not pid:
        return None
    try:
        import psutil

        return float(psutil.Process(int(pid)).create_time())
    except Exception:
        return None


@dataclass(frozen=True)
class RegistryEntry:
    task_name: str
    agent_path: AgentPath
    parent_path: AgentPath | None
    run_id: str
    artifact_dir: str
    task_dir: str
    state_path: str
    status: str = "running"
    pid: int | None = None
    pid_create_time: float | None = None
    parent_session_id: str | None = None
    last_task_message: str | None = None
    turn_status: str | None = None
    process_status: str | None = None
    parent_permission_mode: str | None = None
    permission_profile: str | None = None
    permission_options: dict | None = None
    agent_type: str | None = None
    role_source_path: str | None = None
    background: bool = True
    ipc_mode: str | None = None
    effective_ipc_mode: str | None = None
    ipc_fallback_reason: str | None = None
    isolation: str | None = None
    worktree_path: str | None = None
    previous_status: str | None = None
    closed_status: str | None = None
    created_at: str | None = None
    updated_at: str | None = None
    closed_at: str | None = None


class SubagentRegistry:
    def __init__(
        self,
        registry_dir,
        *,
        max_depth=DEFAULT_MAX_DEPTH,
        max_active_agents=DEFAULT_MAX_ACTIVE_AGENTS,
        process_exists=None,
        process_identity=None,
        startup_grace_s=DEFAULT_STARTUP_GRACE_S,
    ):
        self.registry_dir = Path(registry_dir)
        self.path = self.registry_dir / "registry.json"
        # Every read-modify-write on registry.json goes through this lock. One shared
        # registry.json backs the whole agent tree, and each child that spawns children of
        # its own builds a SubagentManager (agentmain.py) and thus a second writer, so the
        # unlocked _load/_save cycle lost rows and handed the same run_id to several agents.
        self.lock_path = self.registry_dir / "registry.json.lock"
        self.max_depth = int(max_depth)
        self.max_active_agents = int(max_active_agents)
        # Liveness probe for the active-agent cap. Without it the cap counts rows, and a row
        # only becomes "closed" when close_agent runs — crashes, kills and reboots leave it
        # behind forever, so the cap eventually refuses every spawn.
        self.process_exists = process_exists or _default_process_exists
        self.process_identity = process_identity or _default_process_identity
        self.startup_grace_s = float(startup_grace_s)

    @contextmanager
    def _write_locked(self):
        """Serialize one whole read-modify-write cycle; never nest these."""
        self.registry_dir.mkdir(parents=True, exist_ok=True)
        with cross_process_lock(self.lock_path):
            yield

    def create_child(
        self,
        parent_path,
        task_name,
        task_dir,
        state_path,
        *,
        pid=None,
        pid_create_time=None,
        parent_session_id=None,
        last_task_message=None,
        parent_permission_mode=None,
        permission_profile=None,
        permission_options=None,
        agent_type=None,
        role_source_path=None,
        background=True,
        ipc_mode=None,
        effective_ipc_mode=None,
        ipc_fallback_reason=None,
        isolation=None,
        worktree_path=None,
    ):
        parent = _coerce_agent_path(parent_path)
        with self._write_locked():
            # Name allocation, run_no allocation and the row write must share one lock: a gap
            # between them is exactly how two callers walked away with the same run_id, and
            # run_id derives artifact_dir, the realtime channel address and the authkey path.
            child_name = self._unique_child_name_unlocked(parent, task_name)
            agent_path = parent.join(child_name)
            data = self._load()
            self._check_tree_limits(data, agent_path)
            data.setdefault("next_run_no", 1)
            run_no = int(data["next_run_no"])
            data["next_run_no"] = run_no + 1
            created_at = now_iso()
            run_id = f"run_{run_no:06d}"
            entry = {
                "task_name": child_name,
                "agent_path": str(agent_path),
                "parent_path": str(parent),
                "run_id": run_id,
                "artifact_dir": str(self.registry_dir / "runs" / run_id),
                "task_dir": str(Path(task_dir)),
                "state_path": str(Path(state_path)),
                "status": "running",
                "pid": pid,
                "pid_create_time": pid_create_time,
                "parent_session_id": parent_session_id,
                "last_task_message": last_task_message,
                "turn_status": None,
                "process_status": None,
                "parent_permission_mode": parent_permission_mode,
                "permission_profile": permission_profile,
                "permission_options": dict(permission_options or {}),
                "agent_type": agent_type,
                "role_source_path": role_source_path,
                "background": bool(background),
                "ipc_mode": ipc_mode,
                "effective_ipc_mode": effective_ipc_mode,
                "ipc_fallback_reason": ipc_fallback_reason,
                "isolation": isolation,
                "worktree_path": worktree_path,
                "previous_status": None,
                "closed_status": None,
                "created_at": created_at,
                "updated_at": created_at,
                "closed_at": None,
            }
            data["agents"][str(agent_path)] = entry
            self._save(data)
            return _entry_from_dict(entry)

    def get(self, agent_path):
        path = str(_coerce_agent_path(agent_path))
        data = self._load()
        try:
            return _entry_from_dict(data["agents"][path])
        except KeyError:
            raise FileNotFoundError(path)

    def list_agents(self, path_prefix=None, include_closed=False):
        data = self._load()
        prefix = str(_coerce_agent_path(path_prefix)) if path_prefix else None
        entries = []
        for raw in data.get("agents", {}).values():
            path = raw.get("agent_path") or ""
            if prefix and not (path == prefix or path.startswith(prefix + "/")):
                continue
            if not include_closed and raw.get("status") == "closed":
                continue
            entries.append(_entry_from_dict(raw))
        return sorted(entries, key=lambda entry: str(entry.agent_path))

    def list_result_view(self, path_prefix=None):
        """Return active and closed registry rows for durable result discovery."""
        return self.list_agents(path_prefix=path_prefix, include_closed=True)

    def descendants(self, agent_path, include_closed=False):
        """Rows strictly below ``agent_path``, deepest first.

        Deepest-first is the cascade close order: a child must be closed before the parent
        that spawned it, or the parent's shutdown races its own children's writes. The `+ "/"`
        in list_agents' prefix test is what keeps /root/a from sweeping up /root/ab.
        """
        prefix = str(_coerce_agent_path(agent_path))
        entries = [e for e in self.list_agents(prefix, include_closed=include_closed) if str(e.agent_path) != prefix]
        return sorted(entries, key=lambda entry: (-len(entry.agent_path.segments), str(entry.agent_path)))

    def update(self, agent_path, **updates):
        path = str(_coerce_agent_path(agent_path))
        with self._write_locked():
            data = self._load()
            if path not in data["agents"]:
                raise FileNotFoundError(path)
            raw = dict(data["agents"][path])
            for key, value in updates.items():
                if not hasattr(RegistryEntry, "__dataclass_fields__") or key in RegistryEntry.__dataclass_fields__:
                    raw[key] = value
            raw["updated_at"] = now_iso()
            data["agents"][path] = raw
            self._save(data)
            return _entry_from_dict(raw)

    def mark_closed(self, agent_path, *, previous_status, closed_status):
        path = str(_coerce_agent_path(agent_path))
        with self._write_locked():
            data = self._load()
            if path not in data["agents"]:
                raise FileNotFoundError(path)
            raw = dict(data["agents"][path])
            now = now_iso()
            raw.update(
                {
                    "status": "closed",
                    "previous_status": previous_status,
                    "closed_status": closed_status,
                    "closed_at": now,
                    "updated_at": now,
                }
            )
            data["agents"][path] = raw
            self._save(data)
            return _entry_from_dict(raw)

    def mark_running(self, agent_path, *, pid=None, turn_status="pending", process_status="alive", pid_create_time=None):
        path = str(_coerce_agent_path(agent_path))
        with self._write_locked():
            data = self._load()
            if path not in data["agents"]:
                raise FileNotFoundError(path)
            raw = dict(data["agents"][path])
            raw.update(
                {
                    "status": "running",
                    "pid": pid,
                    "pid_create_time": pid_create_time,
                    "turn_status": turn_status,
                    "process_status": process_status,
                    "previous_status": raw.get("status"),
                    "closed_status": None,
                    "closed_at": None,
                    "updated_at": now_iso(),
                }
            )
            data["agents"][path] = raw
            self._save(data)
            return _entry_from_dict(raw)

    def release_agent_slot(self, agent_path, *, pid=None, pid_create_time=None):
        """Explicitly release a slot, Codex-style (``release_spawned_thread``).

        The caller is the child itself, so this is an identity claim rather than a guess: the
        row is only closed when the caller still owns it. A stale child that lingers past its
        replacement (or a pid that was recycled onto another GA process) must not close a row
        that no longer describes it.
        """
        path = str(_coerce_agent_path(agent_path))
        with self._write_locked():
            data = self._load()
            raw = data.get("agents", {}).get(path)
            if raw is None or raw.get("status") == "closed":
                return None
            if not self._row_matches_caller(raw, pid=pid, pid_create_time=pid_create_time):
                return None
            now = now_iso()
            raw = dict(raw)
            raw.update(
                {
                    "status": "closed",
                    "previous_status": raw.get("status"),
                    "closed_status": "released",
                    "closed_at": now,
                    "updated_at": now,
                }
            )
            data["agents"][path] = raw
            self._save(data)
            return _entry_from_dict(raw)

    @staticmethod
    def _row_matches_caller(raw, *, pid=None, pid_create_time=None):
        if pid is None:
            return raw.get("pid") is None
        try:
            if int(raw.get("pid")) != int(pid):
                return False
        except (TypeError, ValueError):
            return False
        recorded = raw.get("pid_create_time")
        if recorded is None or pid_create_time is None:
            # No identity on one side: the pid match is the strongest evidence available.
            return True
        try:
            return abs(float(recorded) - float(pid_create_time)) <= 1.0
        except (TypeError, ValueError):
            return True

    def residency_eviction_candidates(self, *, exclude=()):
        """Idle, finished rows that may be unloaded to make room, least-recently-used first.

        Codex does not refuse a spawn while an idle finished agent is resident: it unloads the
        oldest one (``control/residency.rs::try_unload_one_resident``). Its ``is_unloadable``
        is "completed/errored/interrupted and no active turn and no pending mailbox". The
        registry cannot unload an agent itself — that means signalling a process and rewriting
        state — so it returns the ordered candidates and the manager performs the eviction.

        Only genuinely reaped-dead and idle rows qualify: a row that still owns a slot and is
        mid-turn must never be evicted to satisfy a new spawn.
        """
        excluded = {str(_coerce_agent_path(item)) for item in exclude or ()}
        rows = []
        for path, raw in (self._load().get("agents", {}) or {}).items():
            if path in excluded or raw.get("status") == "closed":
                continue
            if not self._row_is_live(raw):
                continue
            if str(raw.get("turn_status") or "").lower() not in {"completed", "errored", "interrupted"}:
                continue
            if str(raw.get("process_status") or "").lower() not in {"waiting_reply", "exited", "shutdown", "killed"}:
                continue
            rows.append((str(raw.get("updated_at") or ""), path, _entry_from_dict(raw)))
        rows.sort(key=lambda item: (item[0], item[1]))
        return [entry for _stamp, _path, entry in rows]

    def _check_tree_limits(self, data, agent_path):
        # Depth counts subagent hops, so /root/a is depth 1 and /root itself is not an agent.
        depth = len(agent_path.segments) - 1
        if self.max_depth > 0 and depth > self.max_depth:
            raise SubagentTreeLimitError(
                f"agent tree depth limit exceeded: {agent_path} would be depth {depth}, max depth is {self.max_depth}",
                reason="depth",
            )
        if self.max_active_agents > 0:
            active, reaped = self._reap_stale_agents(data)
            if active >= self.max_active_agents:
                # The reap happened even though this spawn is refused; persist it here or the
                # rejection path would keep re-discovering the same dead rows on every attempt.
                if reaped:
                    self._save(data)
                raise SubagentTreeLimitError(
                    f"active agent limit exceeded: {active} agents already active, max active is {self.max_active_agents}",
                    reason="active_limit",
                )

    def _row_is_live(self, raw):
        """Decide whether a registry row still owns an active slot.

        Codex tracks this in memory with an atomic slot plus an id-keyed table, so a slot is
        released by identity rather than by guessing at a pid. A cross-process file registry
        has no such handle, so the two things it *can* know authoritatively stand in:

        * what the child said about itself — ``process_status`` is written by the child, and a
          terminal value means the process is done no matter what the pid is doing now;
        * process identity — pid plus start time, because Windows recycles pids and a bare
          ``pid_exists`` happily accepts an unrelated process.

        ``pid is None`` stays "starting" only for ``startup_grace_s``; a crash between
        ``create_child`` and Popen used to leak that slot permanently.
        """
        status = str(raw.get("process_status") or "").strip().lower()
        if status in TERMINAL_PROCESS_STATUSES:
            return False
        pid = raw.get("pid")
        if not pid:
            return not self._startup_grace_expired(raw)
        try:
            existed = bool(self.process_exists(pid))
        except Exception:
            # "Cannot tell" has to mean "alive": reaping a live agent's row would drop it
            # out of list_agents/wait_agents, trading a guard problem for a correctness one.
            return True
        if not existed:
            return False
        recorded = raw.get("pid_create_time")
        if recorded is None:
            # Older rows predate identity tracking; fall back to the bare pid probe.
            return True
        try:
            current = self.process_identity(pid)
        except Exception:
            return True
        if current is None:
            # The probe cannot speak to identity. Keep the row rather than reap a live agent.
            return True
        try:
            return abs(float(current) - float(recorded)) <= 1.0
        except (TypeError, ValueError):
            return True

    def _startup_grace_expired(self, raw):
        if self.startup_grace_s <= 0:
            return True
        stamp = raw.get("created_at") or raw.get("updated_at")
        if not stamp:
            return False
        try:
            created = datetime.fromisoformat(str(stamp))
        except (TypeError, ValueError):
            return False
        if created.tzinfo is None:
            created = created.astimezone()
        now = datetime.now(created.tzinfo)
        return (now - created).total_seconds() >= self.startup_grace_s

    def _reap_stale_agents(self, data):
        """Close rows that no longer own a slot; return ``(live_active_count, reaped_count)``.

        Mutates ``data`` in place so a successful spawn persists the reap in its own save.
        Rows are closed with ``closed_status="stale"`` rather than deleted, because the row is
        the only remaining evidence that the agent crashed.
        """
        active = 0
        reaped = 0
        now = None
        for raw in data.get("agents", {}).values():
            if raw.get("status") == "closed":
                continue
            if self._row_is_live(raw):
                active += 1
                continue
            now = now or now_iso()
            raw.update({"status": "closed", "previous_status": raw.get("status"), "closed_status": "stale", "closed_at": now, "updated_at": now})
            reaped += 1
        return active, reaped

    def reject_if_live(self, agent_path):
        """Raise ``SubagentNameConflictError`` if ``agent_path`` is an agent that is still live.

        Exposed because the caller has to know the answer *before* it derives a task dir: the
        manager's task dirs are named after the task name, so letting create_child rename the
        conflict away would hand the new agent the old agent's directory.
        """
        path = str(_coerce_agent_path(agent_path))
        raw = self._load().get("agents", {}).get(path)
        if raw is None or not self._is_live_row(raw):
            return None
        raise SubagentNameConflictError(self._name_conflict_message(path, raw), agent_path=path)

    @staticmethod
    def _name_conflict_message(path, raw):
        # Names the alternatives on purpose: a bare "already exists" makes the model retry with
        # a mangled name instead of reusing the agent it already has.
        return (
            f"agent path `{path}` already exists and is still live "
            f"(status={raw.get('status')}, process_status={raw.get('process_status')}, "
            f"pid={raw.get('pid')}). Use followup_task to give it more work, "
            f"close_agent to end it first, or pick a different task_name."
        )

    def _unique_child_name_unlocked(self, parent, task_name):
        """Caller must already hold the write lock — see create_child.

        Refuses when the requested name belongs to a live agent, and only then: a closed or
        crashed row still owns its task dir and artifacts, so reusing that exact name would
        clobber the only evidence of what it did. This is the authoritative check — callers may
        pre-check via reject_if_live, but only this one runs under the write lock.
        """
        base = parent.join(task_name).name
        data = self._load()
        taken = data.get("agents", {})
        requested = str(parent.join(base))
        existing = taken.get(requested)
        if existing is not None and self._is_live_row(existing):
            raise SubagentNameConflictError(self._name_conflict_message(requested, existing), agent_path=requested)
        if existing is None:
            return base
        index = 1
        while True:
            candidate = f"{base}_{index}"
            if str(parent.join(candidate)) not in taken:
                return candidate
            index += 1

    def _is_live_row(self, raw):
        """A row is live when it is not closed and still owns its slot.

        Mirrors _reap_stale_agents' liveness rule, including "cannot tell means alive": guessing
        dead here would silently reuse a running agent's name, which is the defect being fixed.
        """
        if raw.get("status") == "closed":
            return False
        return self._row_is_live(raw)

    def _load(self):
        data = read_json_or_none(self.path) or {}
        data.setdefault("schema_version", 1)
        data.setdefault("next_run_no", 1)
        data.setdefault("agents", {})
        return data

    def _save(self, data):
        data["updated_at"] = now_iso()
        atomic_write_json(self.path, data)


def _coerce_agent_path(value):
    if isinstance(value, AgentPath):
        return value
    return AgentPath.parse(value)


def _entry_from_dict(raw):
    parent_path = raw.get("parent_path")
    return RegistryEntry(
        task_name=raw.get("task_name") or AgentPath.parse(raw["agent_path"]).name,
        agent_path=AgentPath.parse(raw["agent_path"]),
        parent_path=AgentPath.parse(parent_path) if parent_path else None,
        run_id=raw.get("run_id") or "run_000000",
        artifact_dir=raw.get("artifact_dir") or raw.get("task_dir") or "",
        task_dir=raw.get("task_dir") or raw.get("artifact_dir") or "",
        state_path=raw.get("state_path") or "",
        status=raw.get("status") or "running",
        pid=raw.get("pid"),
        pid_create_time=raw.get("pid_create_time"),
        parent_session_id=raw.get("parent_session_id"),
        last_task_message=raw.get("last_task_message"),
        turn_status=raw.get("turn_status"),
        process_status=raw.get("process_status"),
        parent_permission_mode=raw.get("parent_permission_mode"),
        permission_profile=raw.get("permission_profile"),
        permission_options=raw.get("permission_options") or {},
        agent_type=raw.get("agent_type"),
        role_source_path=raw.get("role_source_path"),
        background=bool(raw.get("background", True)),
        ipc_mode=raw.get("ipc_mode"),
        effective_ipc_mode=raw.get("effective_ipc_mode"),
        ipc_fallback_reason=raw.get("ipc_fallback_reason"),
        isolation=raw.get("isolation"),
        worktree_path=raw.get("worktree_path"),
        previous_status=raw.get("previous_status"),
        closed_status=raw.get("closed_status"),
        created_at=raw.get("created_at"),
        updated_at=raw.get("updated_at"),
        closed_at=raw.get("closed_at"),
    )
