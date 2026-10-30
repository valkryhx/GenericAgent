"""Session-scoped goal state (Stage F, phase 0).

Mirrors Claude Code 2.1.303 ``activeGoal`` (m0274) and the ``/goal`` command
shape (m0274:78/80 + m1247/m1927). Phase 0 only covers the data model, the
``/goal`` command parser and the met/failed/not_met interpretation of an
evaluator verdict. The Stop hook wiring lives in phase 1 and reuses the
existing hooks runtime (stages A/B/D); this module stays free of I/O so it can
be unit-tested in isolation.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, replace
from typing import Any

# Reasonable length caps. CC uses 4000 for a user-typed /goal condition and 500
# for a model-proposed one (m0274:78 GNt / m0418:12 Vjn). User-typed share the
# larger cap for now; the ProposeGoal tool is a later phase.
GOAL_CONDITION_MAX = 4000
GOAL_PROPOSAL_MAX = 500

# Clear/stop aliases accepted by ``/goal clear`` (m0274:80 nmt).
GOAL_CLEAR_WORDS = frozenset({"clear", "stop", "off", "reset", "none", "cancel"})

# Origins recorded on ActiveGoal.origin (m0274 queuedGoalOrigin).
ORIGIN_USER = "user"
ORIGIN_PROPOSAL_DIRECT = "proposal_direct"
ORIGIN_PROPOSAL_APPROVED = "proposal_approved"
# Set when a goal is re-armed on session resume (CC origin "restored").
ORIGIN_RESTORED = "restored"


@dataclass(frozen=True)
class ActiveGoal:
    """A single active goal for the session (at most one at a time)."""

    condition: str
    iterations: int = 0
    set_at: float = field(default_factory=time.time)
    origin: str = ORIGIN_USER
    tokens_at_start: int = 0
    last_reason: str | None = None
    deferred_since: float | None = None
    checkin_count: int = 0
    last_deferral_pass_at: float | None = None
    idle_checkin_count: int = 0

    def bumped(self, reason: str | None) -> "ActiveGoal":
        """Return a copy with one more evaluation and the latest reason."""
        return replace(self, iterations=self.iterations + 1, last_reason=reason)


@dataclass(frozen=True)
class GoalStatus:
    """Interpreted result of one goal evaluation (CC goal_status attachment)."""

    met: bool
    failed: bool = False
    reason: str = ""
    impossible: bool = False
    error_kind: str | None = None  # "evaluator_error" / "evaluator_timeout"


def normalize_condition(raw: str) -> str:
    """Collapse weird line separators and strip surrounding whitespace."""
    text = str(raw or "")
    for sep in ("\r\n", "\r", "\u2028", "\u2029"):
        text = text.replace(sep, "\n")
    return text.strip()


@dataclass(frozen=True)
class GoalCommand:
    """Parsed result of a ``/goal`` invocation.

    ``kind`` is one of ``"status"`` (show current), ``"clear"`` (remove) or
    ``"set"`` (install/overwrite with ``condition``). ``error`` is set when the
    input was rejected (e.g. a too-long condition) and must be surfaced as-is.
    """

    kind: str
    condition: str | None = None
    error: str | None = None


def parse_goal_command(arg: str, *, max_len: int = GOAL_CONDITION_MAX) -> GoalCommand:
    """Parse the text after ``/goal`` into a command (no leading slash)."""
    text = normalize_condition(arg)
    if not text:
        return GoalCommand(kind="status")
    lowered = text.lower()
    if lowered in GOAL_CLEAR_WORDS:
        return GoalCommand(kind="clear")
    if len(text) > max_len:
        return GoalCommand(
            kind="error",
            error=(
                "Goal condition is limited to %d characters (got %d)"
                % (max_len, len(text))
            ),
        )
    return GoalCommand(kind="set", condition=text)


def interpret_verdict(aggregate: Any) -> GoalStatus:
    """Interpret a Stop-gate HookAggregate for goal accounting.

    The hooks evaluator channel (stage D, models.py) already turns
    ``{ok, reason, impossible}`` into aggregate state:
      - ok:true            -> not blocked, reason kept
      - ok:false+impossible-> not blocked, reason kept
      - ok:false           -> decision='block' (blocked)
    So met and impossible are indistinguishable at the aggregate level; we read
    the raw per-hook verdict to tell them apart. Failures (invalid_output /
    error / timeout) never count as met.

    Returns a GoalStatus; ``error_kind`` is set for evaluator failures and the
    caller should treat those as "do not block, do not clear" (let the turn end).
    """
    if aggregate is None:
        return GoalStatus(met=False, error_kind="evaluator_error")

    verdicts = []
    for result in getattr(aggregate, "results", []) or []:
        handler = getattr(result, "handler", None)
        if handler is None or getattr(handler, "kind", "") not in ("prompt", "agent"):
            continue
        if not getattr(handler, "output_schema", None):
            continue
        verdicts.append(result)

    if not verdicts:
        # No structured evaluator result at all -> not a successful evaluation.
        return GoalStatus(met=False, error_kind="evaluator_error")

    latest = verdicts[-1]
    status = str(getattr(latest, "status", "") or "")
    if status == "timeout":
        return GoalStatus(met=False, error_kind="evaluator_timeout")
    if status not in ("success", "blocking"):
        return GoalStatus(met=False, error_kind="evaluator_error")

    output = getattr(latest, "output", None)
    if not isinstance(output, dict):
        return GoalStatus(met=False, error_kind="evaluator_error")
    ok = output.get("ok")
    reason = str(output.get("reason") or "")
    if ok is True:
        return GoalStatus(met=True, reason=reason)
    if output.get("impossible") is True:
        return GoalStatus(met=False, failed=True, impossible=True, reason=reason)
    if ok is False:
        return GoalStatus(met=False, reason=reason)
    return GoalStatus(met=False, error_kind="evaluator_error")
