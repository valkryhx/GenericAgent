"""One-line goal nudge with cooldown (Stage F, phase 5, S3).

Aligned with CC's single ``goal-command-nudge`` (m1837:1227-1231): a passive
one-line suggestion, never a dialog, shown at most once per
``GA_GOAL_NUDGE_COOLDOWN_SESSIONS`` sessions (default 3) and only when it is
relevant (no active goal). It is OFF by default (``GA_GOAL_NUDGE=0``) so the
feature adds zero noise unless a user opts in.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

DEFAULT_COOLDOWN_SESSIONS = 3

_TRUTHY = frozenset({"1", "true", "yes", "on"})
_TEXT = "用 /goal 设定一个目标 —— GA 会一直工作到它达成"


def nudge_enabled() -> bool:
    """Default OFF: opt in with GA_GOAL_NUDGE=1."""
    raw = os.environ.get("GA_GOAL_NUDGE")
    if raw is None:
        return False
    return str(raw).strip().lower() in _TRUTHY


def cooldown_sessions() -> int:
    raw = os.environ.get("GA_GOAL_NUDGE_COOLDOWN_SESSIONS")
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return DEFAULT_COOLDOWN_SESSIONS
    return max(1, value)


def nudge_text() -> str:
    return _TEXT


def should_nudge(
    *,
    enabled: bool,
    has_active_goal: bool,
    sessions_since_last: int,
    cooldown_sessions: int = DEFAULT_COOLDOWN_SESSIONS,
    relevant: bool = True,
) -> bool:
    """Pure gate. Any of these short-circuit to no nudge.

    - feature disabled
    - a goal is already active
    - not "relevant" (e.g. hooks disabled / restricted env)
    - still inside the cooldown window
    """
    if not enabled or has_active_goal or not relevant:
        return False
    window = max(1, int(cooldown_sessions))
    return int(sessions_since_last) >= window


def _state_path(data_root: Any = None) -> Path:
    if data_root:
        return Path(str(data_root)) / "goal_nudge_state.json"
    from genericagent import paths

    return Path(str(paths.data_root())) / "goal_nudge_state.json"


def read_state(data_root: Any = None) -> dict:
    try:
        path = _state_path(data_root)
        if not path.is_file():
            return {"sessions": 0, "last_nudged_session": None}
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, dict):
            return {"sessions": 0, "last_nudged_session": None}
        return {
            "sessions": int(data.get("sessions") or 0),
            "last_nudged_session": data.get("last_nudged_session"),
        }
    except Exception:
        return {"sessions": 0, "last_nudged_session": None}


def write_state(state: dict, data_root: Any = None) -> None:
    try:
        path = _state_path(data_root)
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(state, fh, ensure_ascii=False)
    except Exception:
        pass


def evaluate(data_root: Any = None, *, has_active_goal: bool = False, relevant: bool = True) -> str | None:
    """Advance the session counter and return the nudge text once, or None.

    Called once per session start. The counter resets when a nudge fires so the
    next one waits a full cooldown window again.
    """
    enabled = nudge_enabled()
    state = read_state(data_root)
    state["sessions"] = int(state.get("sessions") or 0) + 1
    fire = should_nudge(
        enabled=enabled,
        has_active_goal=has_active_goal,
        sessions_since_last=state["sessions"],
        cooldown_sessions=cooldown_sessions(),
        relevant=relevant,
    )
    if fire:
        state["sessions"] = 0
        state["last_nudged_session"] = state.get("last_nudged_session")
    if enabled:
        write_state(state, data_root)
    return nudge_text() if fire else None
