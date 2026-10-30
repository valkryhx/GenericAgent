"""Goal wake scheduler (Stage F, phases 2-B + 3).

Gives an active goal the ability to bring the model back on its own when the
user is not typing:

- **2-B deferral check-in**: the goal evaluation was deferred because
  background sub-agents were still running. After a delay we check in; if the
  background work is still running we list it, otherwise we tell the model to
  continue. Idle check-ins are capped so a stuck goal cannot ping forever.
- **3 interruption retry**: a turn ended before the goal could be evaluated
  (step limit / provider or tool error). We re-prompt the model to continue,
  a bounded number of times, then pause and tell the user.

This module is deliberately pure/near-pure: timers and the clock are injectable
so the whole decision surface is unit-testable without sleeping or hitting the
network. Numbers mirror Claude Code 2.1.303 ``m0671``:

    Du = 30 (min, base check-in)  Uu = 2 (backoff cap exponent)  Lu = 3 (idle cap)
    Us = [60000, 300000, 900000] ms (retry backoff)  Ds = 3 (retry cap)

Everything is OFF unless ``GA_GOAL_WAKE`` is truthy, so default behaviour is
exactly what it was before this module existed.
"""

from __future__ import annotations

import os
import threading
import time
from typing import Any, Callable, Optional

# --- CC 303 m0671 constants (do not invent new numbers) ---------------------
CHECKIN_BASE_MINUTES = 30          # Du
CHECKIN_BACKOFF_CAP_EXP = 2        # Uu
CHECKIN_IDLE_CAP = 3               # Lu
RETRY_DELAYS_MS = (60000, 300000, 900000)  # Us : 1 / 5 / 15 min
RETRY_MAX = 3                      # Ds

_IDLE_PAUSED_SUFFIX = " \u00b7 idle check-ins paused until your next message"


# --- config (read the process env, matching every other GA_* switch) --------
def _truthy(raw: object) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def is_enabled() -> bool:
    """Master switch. Default OFF: opt in with ``GA_GOAL_WAKE=1``."""
    return _truthy(os.environ.get("GA_GOAL_WAKE"))


def checkin_base_minutes() -> int:
    try:
        v = int(str(os.environ.get("GA_GOAL_CHECKIN_MINUTES", CHECKIN_BASE_MINUTES)).strip())
        return v if v > 0 else CHECKIN_BASE_MINUTES
    except (TypeError, ValueError):
        return CHECKIN_BASE_MINUTES


def checkin_idle_cap() -> int:
    try:
        v = int(str(os.environ.get("GA_GOAL_CHECKIN_IDLE_CAP", CHECKIN_IDLE_CAP)).strip())
        return v if v >= 0 else CHECKIN_IDLE_CAP
    except (TypeError, ValueError):
        return CHECKIN_IDLE_CAP


def retry_max() -> int:
    try:
        v = int(str(os.environ.get("GA_GOAL_RETRY_MAX", RETRY_MAX)).strip())
        return v if v >= 0 else RETRY_MAX
    except (TypeError, ValueError):
        return RETRY_MAX


# --- pure delay math --------------------------------------------------------
def checkin_delay_ms(count: int, base_minutes: Optional[int] = None) -> int:
    """Backoff for the *count*-th check-in: base * 2**min(count, cap)."""
    base = (base_minutes if base_minutes is not None else checkin_base_minutes()) * 60000
    return int(base * (2 ** min(max(count, 0), CHECKIN_BACKOFF_CAP_EXP)))


def retry_delay_ms(streak: int, jitter: float = 0.0) -> int:
    """Delay before the *streak*-th retry, with optional +/- jitter fraction."""
    idx = min(max(streak, 0), len(RETRY_DELAYS_MS) - 1)
    base = RETRY_DELAYS_MS[idx]
    if jitter:
        base = int(base * (1 + jitter))
    return base


# --- message builders (mirror CC Bi()/ju()/retry copy) ----------------------
def _cond(agent: Any) -> str:
    goal = getattr(agent, "active_goal", None)
    cond = getattr(goal, "condition", "") if goal is not None else ""
    return str(cond or "the goal")


def build_checkin_message(agent: Any, deferred_ms: int, tasks: list[str]) -> dict:
    """Return {"summary", "body"} for a 2-B check-in."""
    minutes = max(1, round(deferred_ms / 60000))
    cond = _cond(agent)
    if not tasks:
        return {
            "summary": "Goal check-in: background work no longer running",
            "body": (
                f"{cond} is still active. Its evaluation was deferred for {minutes} min "
                "while background work ran, and that work is no longer running (it "
                "finished or was stopped without reporting back). Continue toward the goal."
            ),
        }
    listing = "\n".join(f"- {t}" for t in tasks)
    return {
        "summary": "Goal check-in: background work still running",
        "body": (
            f"{cond} is still active, and evaluation has been deferred for {minutes} min "
            f"because background work is still running:\n{listing}\n"
            "Check on their progress (e.g. read their output). If they are progressing, "
            "say so briefly and keep waiting; if they are stuck or no longer needed, fix "
            "or stop them and continue toward the goal."
        ),
    }


def build_retry_message(agent: Any, cause: str) -> dict:
    return {
        "summary": "Goal check-in: continuing after the last turn ended early",
        "body": (
            f"{_cond(agent)} is still active. The last turn ended before the goal could "
            f"be evaluated: {cause}. Continue toward the goal."
        ),
    }


def build_paused_message(cause: str, retries: int) -> dict:
    return {
        "summary": "Goal paused after automatic retries",
        "body": (
            f"Goal paused after {retries} automatic retries \u00b7 {cause} \u00b7 "
            "send a message to continue"
        ),
    }


# --- scheduler --------------------------------------------------------------
WakeCallback = Callable[[str], bool]
TimerFactory = Callable[[float, Callable[[], None]], Any]


def _default_timer_factory(delay_s: float, cb: Callable[[], None]) -> threading.Timer:
    t = threading.Timer(max(0.0, delay_s), cb)
    t.daemon = True
    t.start()
    return t


class GoalWakeScheduler:
    """One per agent. Holds at most one pending timer at a time (single-flight).

    ``fire`` re-checks that the session and goal identity are unchanged before
    delivering, so a timer armed for an old goal/session can never wake a new
    one (defends against resume, /goal clear, and goal replacement).
    """

    def __init__(
        self,
        agent: Any,
        wake_callback: Optional[WakeCallback] = None,
        *,
        timer_factory: TimerFactory = _default_timer_factory,
        has_in_flight: Optional[Callable[[Any], list[str]]] = None,
        on_event: Optional[Callable[[str, str], None]] = None,
        jitter: float = 0.0,
    ) -> None:
        self.agent = agent
        self.wake_callback = wake_callback
        self._timer_factory = timer_factory
        self._has_in_flight = has_in_flight
        self._on_event = on_event
        self._jitter = jitter
        self._timer: Any = None
        self._kind: Optional[str] = None      # 'checkin' | 'retry'
        self._session_id: Optional[str] = None
        self._goal_set_at: Optional[float] = None
        self._cause: str = ""
        self._deferred_ms: int = 0
        self.retry_streak = 0
        self.checkin_count = 0
        self.idle_count = 0

    # -- state helpers --
    def _emit(self, event: str, outcome: str) -> None:
        if self._on_event is not None:
            try:
                self._on_event(event, outcome)
            except Exception:
                pass

    def _snapshot(self) -> tuple[Optional[str], Optional[float]]:
        sid = getattr(self.agent, "session_id", None)
        goal = getattr(self.agent, "active_goal", None)
        set_at = getattr(goal, "set_at", None) if goal is not None else None
        return sid, set_at

    def _cancel_timer(self) -> None:
        if self._timer is not None:
            try:
                self._timer.cancel()
            except Exception:
                pass
            self._timer = None

    def pending(self) -> bool:
        return self._timer is not None

    def cancel(self, reason: str = "") -> None:
        """Drop any pending timer (new user msg / goal clear / resume / shutdown)."""
        was = self._kind
        self._cancel_timer()
        self._kind = None
        if was:
            self._emit("goal_checkin" if was == "checkin" else "goal_interruption",
                       "cancelled" if not reason else f"cancelled:{reason}")

    # -- arming --
    def arm_checkin(self, delay_ms: Optional[int] = None) -> bool:
        goal = getattr(self.agent, "active_goal", None)
        if goal is None:
            return False
        self._cancel_timer()
        delay = checkin_delay_ms(self.checkin_count) if delay_ms is None else int(delay_ms)
        self._kind = "checkin"
        self._session_id, self._goal_set_at = self._snapshot()
        self._deferred_ms = delay
        try:
            self._timer = self._timer_factory(delay / 1000.0, self.fire)
        except Exception:
            self._timer = None
            self._emit("goal_checkin", "arm_failed")
            return False
        self._emit("goal_checkin", "armed")
        return True

    def arm_retry(self, cause: str = "", delay_ms: Optional[int] = None) -> bool:
        goal = getattr(self.agent, "active_goal", None)
        if goal is None:
            return False
        if self.retry_streak >= retry_max():
            self._emit("goal_interruption", "gave_up")
            self._deliver_paused(cause)
            return False
        self._cancel_timer()
        delay = retry_delay_ms(self.retry_streak, self._jitter) if delay_ms is None else int(delay_ms)
        self._kind = "retry"
        self._cause = cause
        self._session_id, self._goal_set_at = self._snapshot()
        try:
            self._timer = self._timer_factory(delay / 1000.0, self.fire)
        except Exception:
            self._timer = None
            self._emit("goal_interruption", "arm_failed")
            return False
        self._emit("goal_interruption", "armed")
        return True
    def _deliver(self, message: dict) -> bool:
        if self.wake_callback is None:
            return False
        prompt = message.get("summary", "") + "\n\n" + message.get("body", "")
        try:
            return bool(self.wake_callback(prompt))
        except Exception:
            return False

    def _deliver_paused(self, cause: str) -> None:
        msg = build_paused_message(cause or "the last turn ended early", retry_max())
        self._deliver(msg)

    # -- firing --
    def fire(self) -> None:
        """Timer callback: validate identity, then deliver the right message."""
        sid, set_at = self._snapshot()
        if sid != self._session_id or set_at != self._goal_set_at or set_at is None:
            # Session or goal changed since we armed -> stale, drop silently.
            self._timer = None
            self._kind = None
            return
        kind = self._kind
        self._timer = None
        self._kind = None
        if kind == "checkin":
            self._fire_checkin()
        elif kind == "retry":
            self._fire_retry()

    def _fire_checkin(self) -> None:
        tasks: list[str] = []
        if self._has_in_flight is not None:
            try:
                tasks = list(self._has_in_flight(self.agent) or [])
            except Exception:
                tasks = []
        cap = checkin_idle_cap()
        msg = build_checkin_message(self.agent, self._deferred_ms, tasks)
        if tasks:
            self.checkin_count += 1
        else:
            # No background work left and the goal still active -> idle.
            self.idle_count += 1
        if self.idle_count >= cap and not tasks:
            msg = {
                "summary": msg["summary"] + _IDLE_PAUSED_SUFFIX,
                "body": msg["body"] + (
                    " Note: this session will not be woken for another check-in until "
                    "the user sends a message, so say clearly where things stand."
                ),
            }
            self._emit("goal_checkin", "idle_cap_reached")
            # Do not re-arm: idle check-ins are paused until the next user message.
            self._deliver(msg)
            return
        self._emit("goal_checkin", "fired")
        delivered = self._deliver(msg)
        if not delivered:
            self._emit("goal_checkin", "fire_failed")

    def _fire_retry(self) -> None:
        self.retry_streak += 1
        self._emit("goal_interruption", "fired")
        delivered = self._deliver(build_retry_message(self.agent, self._cause))
        if not delivered:
            self._emit("goal_interruption", "fire_failed")
        if self.retry_streak >= retry_max():
            self._emit("goal_interruption", "gave_up")
            self._deliver_paused(self._cause)

    def reset_streaks(self) -> None:
        """Reset retry/idle counters (goal met/cleared/replaced or new user msg)."""
        self.retry_streak = 0
        self.checkin_count = 0
        self.idle_count = 0
