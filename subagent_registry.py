from dataclasses import dataclass
from pathlib import Path

from subagent_agent_path import AgentPath
from subagent_state import atomic_write_json, now_iso, read_json_or_none


DEFAULT_MAX_DEPTH = 3
DEFAULT_MAX_ACTIVE_AGENTS = 8


class SubagentTreeLimitError(RuntimeError):
    """Raised when a spawn would exceed the agent tree depth or active-agent cap.

    Every GA subagent is a separate OS process and can spawn its own children, so an
    unbounded tree burns processes, memory and real LLM spend. Codex guards the same thing
    with AgentRegistry { active_agents, total_count } and reserve_spawn_slot.
    """


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
    def __init__(self, registry_dir, *, max_depth=DEFAULT_MAX_DEPTH, max_active_agents=DEFAULT_MAX_ACTIVE_AGENTS, process_exists=None):
        self.registry_dir = Path(registry_dir)
        self.path = self.registry_dir / "registry.json"
        self.max_depth = int(max_depth)
        self.max_active_agents = int(max_active_agents)
        # Liveness probe for the active-agent cap. Without it the cap counts rows, and a row
        # only becomes "closed" when close_agent runs — crashes, kills and reboots leave it
        # behind forever, so the cap eventually refuses every spawn.
        self.process_exists = process_exists or _default_process_exists

    def create_child(
        self,
        parent_path,
        task_name,
        task_dir,
        state_path,
        *,
        pid=None,
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
        child_name = self._unique_child_name(parent, task_name)
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

    def update(self, agent_path, **updates):
        path = str(_coerce_agent_path(agent_path))
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

    def mark_running(self, agent_path, *, pid=None, turn_status="pending", process_status="alive"):
        path = str(_coerce_agent_path(agent_path))
        data = self._load()
        if path not in data["agents"]:
            raise FileNotFoundError(path)
        raw = dict(data["agents"][path])
        raw.update(
            {
                "status": "running",
                "pid": pid,
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

    def _check_tree_limits(self, data, agent_path):
        # Depth counts subagent hops, so /root/a is depth 1 and /root itself is not an agent.
        depth = len(agent_path.segments) - 1
        if self.max_depth > 0 and depth > self.max_depth:
            raise SubagentTreeLimitError(
                f"agent tree depth limit exceeded: {agent_path} would be depth {depth}, max depth is {self.max_depth}"
            )
        if self.max_active_agents > 0:
            active, reaped = self._reap_stale_agents(data)
            if active >= self.max_active_agents:
                # The reap happened even though this spawn is refused; persist it here or the
                # rejection path would keep re-discovering the same dead rows on every attempt.
                if reaped:
                    self._save(data)
                raise SubagentTreeLimitError(
                    f"active agent limit exceeded: {active} agents already active, max active is {self.max_active_agents}"
                )

    def _reap_stale_agents(self, data):
        """Close rows whose process is gone; return ``(live_active_count, reaped_count)``.

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
            pid = raw.get("pid")
            if not pid:
                # spawn registers the child before Popen, so no pid means "starting", not "dead".
                active += 1
                continue
            try:
                alive = bool(self.process_exists(pid))
            except Exception:
                # "Cannot tell" has to mean "alive": reaping a live agent's row would drop it
                # out of list_agents/wait_agents, trading a guard problem for a correctness one.
                active += 1
                continue
            if alive:
                active += 1
                continue
            now = now or now_iso()
            raw.update({"status": "closed", "previous_status": raw.get("status"), "closed_status": "stale", "closed_at": now, "updated_at": now})
            reaped += 1
        return active, reaped

    def _unique_child_name(self, parent, task_name):
        base = parent.join(task_name).name
        data = self._load()
        if str(parent.join(base)) not in data.get("agents", {}):
            return base
        index = 1
        while True:
            candidate = f"{base}_{index}"
            if str(parent.join(candidate)) not in data.get("agents", {}):
                return candidate
            index += 1

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
