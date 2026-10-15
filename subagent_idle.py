"""Idle notification frames + result budgets for teammates (CC ``m0300`` / ``m1448``).

CC evidence (v2.1.283):

- ``m0300 mat``: when a teammate finishes a turn and goes idle it sends the lead a
  structured ``idle_notification`` frame:
  ``{type, from, timestamp, idleReason, summary, completedTaskId, completedStatus,
  failureReason, result}``.
- ``m0300`` result budgets: a single ``result`` is capped at ``xB=4000`` chars, and a
  whole drain at ``eTo=16000``; overflow is truncated with a hint to ask the agent
  for the rest via SendMessage.
- ``m1448 zo``: if the teammate already sent the lead a successful SendMessage this
  turn, the idle result is suppressed (``suppressed_lead_dm``) so the lead does not
  get the same thing twice.
"""

from __future__ import annotations

import json
from pathlib import Path

from subagent_state import now_iso


# CC `m0300` xB / eTo
RESULT_BUDGET = 4000
DRAIN_BUDGET = 16000

TRUNCATION_HINT = "[result truncated - ask the agent for the rest via send_message]"
DRAIN_TRUNCATION_HINT = (
    "[result truncated - this drain's results exceeded 16000 characters; "
    "ask the agent for it via send_message]"
)


def truncate_result(text, budget=RESULT_BUDGET):
    """Cap one result body, appending CC's truncation hint when it overflows."""
    value = "" if text is None else str(text)
    if len(value) <= budget:
        return value
    return value[:budget] + "\n" + TRUNCATION_HINT


def truncate_drain(results, budget=DRAIN_BUDGET):
    """Cap a whole drain (a sequence of result bodies) at the drain budget."""
    out = []
    used = 0
    for item in results or ():
        value = "" if item is None else str(item)
        remaining = budget - used
        if remaining <= 0:
            out.append(DRAIN_TRUNCATION_HINT)
            break
        if len(value) > remaining:
            out.append(value[:remaining] + "\n" + TRUNCATION_HINT)
            used = budget
            continue
        out.append(value)
        used += len(value)
    return out


def build_idle_notification(
    *,
    from_name,
    idle_reason,
    summary="",
    completed_task_id=None,
    completed_status=None,
    failure_reason=None,
    result=None,
    timestamp=None,
):
    """Build the CC ``m0300 mat`` idle_notification frame."""
    frame = {
        "type": "idle_notification",
        "from": str(from_name or ""),
        "timestamp": timestamp or now_iso(),
        "idleReason": str(idle_reason or "available"),
        "summary": _one_line(summary),
    }
    if completed_task_id is not None:
        frame["completedTaskId"] = str(completed_task_id)
    if completed_status is not None:
        frame["completedStatus"] = str(completed_status)
    if failure_reason:
        frame["failureReason"] = str(failure_reason)
    if result is not None:
        frame["result"] = truncate_result(result)
    return frame


def _one_line(text):
    return " ".join(str(text or "").split())

def sent_lead_dm_since_last_turn(events_path):
    """True when the teammate messaged the lead after its most recent turn.

    Mirrors CC ``m1448 zo``: an explicit lead DM suppresses the duplicate idle result.
    """
    try:
        rows = [
            json.loads(line)
            for line in Path(events_path).read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    except (OSError, json.JSONDecodeError):
        return False
    last_turn = -1
    last_lead = -1
    for index, row in enumerate(rows):
        if row.get("type") == "turn_completed":
            last_turn = index
        if row.get("type") == "lead_message":
            last_lead = index
    return last_lead > last_turn
