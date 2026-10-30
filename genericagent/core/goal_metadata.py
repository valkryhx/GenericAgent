"""Session goal metadata for SDK/UI (Stage F, phase 5, S0).

Mirrors Claude Code 2.1.303 ``Mqr`` (m0696:378-400): a small, shallow-comparable
projection of the active goal that the UI/SDK can render without parsing the
session transcript. This module is pure (no I/O, no emit) so it is trivially
unit-testable; ``app.py`` owns the actual ``hook_emit`` plumbing.
"""

from __future__ import annotations

from typing import Any

# The fields CC shallow-compares before re-pushing metadata (m0696:378 R).
# ``met`` / ``tokens_spent`` / ``duration_ms`` are derived and intentionally
# excluded so a pure token tick does not spam the UI.
DIFF_FIELDS = ("condition", "iterations", "set_at", "tokens_at_start", "last_reason")


def goal_metadata(active_goal: Any, *, current_tokens: int | None = None) -> dict | None:
    """Project an ActiveGoal into the public metadata dict.

    ``tokens_spent`` is ``current_tokens - tokens_at_start`` when the caller can
    supply a cumulative session counter; otherwise 0. The raw ``tokens_at_start``
    anchor is always included so an SDK can compute it itself.
    """
    if active_goal is None:
        return None
    tokens_at_start = _int(getattr(active_goal, "tokens_at_start", 0))
    spent = 0
    if current_tokens is not None:
        spent = max(0, _int(current_tokens) - tokens_at_start)
    meta = {
        "condition": str(getattr(active_goal, "condition", "") or ""),
        "iterations": _int(getattr(active_goal, "iterations", 0)),
        "set_at": _num(getattr(active_goal, "set_at", 0.0)),
        "tokens_at_start": tokens_at_start,
        "tokens_spent": spent,
        "met": False,
    }
    last_reason = getattr(active_goal, "last_reason", None)
    if last_reason is not None:
        meta["last_reason"] = str(last_reason)
    return meta


def differs(previous: dict | None, current: dict | None) -> bool:
    """True when the metadata changed enough to warrant a push (CC shallow diff)."""
    if previous is current:
        return False
    if previous is None or current is None:
        return True
    return any(previous.get(field) != current.get(field) for field in DIFF_FIELDS)


def _int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _num(value: Any) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0
