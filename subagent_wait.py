import re
from dataclasses import dataclass, field


WAIT_CONDITIONS = frozenset(
    {
        "event",
        "turn_terminal",
        "process_terminal",
        "all_terminal",
        "result_available",
        "workflow_terminal",
    }
)
TURN_TERMINAL_STATUSES = frozenset({"completed", "failed", "cancelled", "canceled", "killed", "stale"})
PROCESS_TERMINAL_STATUSES = frozenset({"exited", "shutdown", "killed", "stale"})


@dataclass(frozen=True)
class WaitDecision:
    condition: str
    satisfied: bool
    timed_out: bool = False
    changed_agents: list = field(default_factory=list)
    remaining_targets: list[str] = field(default_factory=list)
    terminal_targets: list[str] = field(default_factory=list)
    result_refs: dict[str, str] = field(default_factory=dict)
    recommended_next_action: str = "wait_agent"
    reason: str = ""


def _value(state, name, default=None):
    return getattr(state, name, default)


def _turn_terminal(state):
    return str(_value(state, "turn_status", "") or "").lower() in TURN_TERMINAL_STATUSES


def _process_terminal(state):
    return str(_value(state, "process_status", "") or "").lower() in PROCESS_TERMINAL_STATUSES


def _result_ref(state):
    result_status = str(_value(state, "result_status", "") or "").lower()
    result_ref = _value(state, "result_ref") or _value(state, "final_output_path")
    if result_status not in {"available", "ready", "persisted"} or not result_ref:
        return None
    return str(result_ref)


def evaluate_wait_condition(states, condition):
    """Evaluate a wait predicate without reading result bodies or mutating state."""
    condition = str(condition or "event").strip().lower()
    if condition not in WAIT_CONDITIONS:
        raise ValueError(f"unsupported wait condition: {condition}")

    observed = list(states or [])
    turn_terminal = [state for state in observed if _turn_terminal(state)]
    process_terminal = [state for state in observed if _process_terminal(state)]
    result_refs = {
        str(_value(state, "task_name")): ref
        for state in observed
        if _value(state, "task_name") and (ref := _result_ref(state))
    }

    if condition == "event":
        satisfied = bool(observed)
        remaining = []
        terminal = []
    elif condition == "turn_terminal":
        satisfied = bool(observed) and len(turn_terminal) == len(observed)
        remaining = [str(_value(state, "task_name")) for state in observed if not _turn_terminal(state)]
        terminal = [str(_value(state, "task_name")) for state in turn_terminal]
    elif condition == "process_terminal":
        satisfied = bool(observed) and len(process_terminal) == len(observed)
        remaining = [str(_value(state, "task_name")) for state in observed if not _process_terminal(state)]
        terminal = [str(_value(state, "task_name")) for state in process_terminal]
    elif condition in {"all_terminal", "workflow_terminal"}:
        satisfied = bool(observed) and len(turn_terminal) == len(observed)
        remaining = [str(_value(state, "task_name")) for state in observed if not _turn_terminal(state)]
        terminal = [str(_value(state, "task_name")) for state in turn_terminal]
    else:
        satisfied = bool(observed) and len(result_refs) == len(observed)
        remaining = [str(_value(state, "task_name")) for state in observed if str(_value(state, "task_name")) not in result_refs]
        terminal = [str(_value(state, "task_name")) for state in observed if str(_value(state, "task_name")) in result_refs]

    next_action = "read_agent_result" if satisfied and result_refs else "wait_agent"
    return WaitDecision(
        condition=condition,
        satisfied=satisfied,
        remaining_targets=remaining,
        terminal_targets=terminal,
        result_refs=result_refs,
        recommended_next_action=next_action,
        reason="wait predicate satisfied" if satisfied else "waiting for remaining targets or persisted results",
    )# --- Workflow runs as waitable entities -------------------------------------
# A dynamic-workflow run (``wf_<32 hex>``) is not a subagent: it has no
# ``temp/<task_name>/state.json`` and no OS process. Before this module grew the
# helpers below, ``wait_agent(condition="workflow_terminal", target=<runId>)``
# silently fell back to the subagent state machine, which synthesised a
# never-terminal state (``turn_status="running"``, ``process_status="exited"``)
# and waited out the whole timeout. That is GA defect A.
#
# CC evidence (``claudecode283/extracted/dynamic-workflow-internals.md`` 8.3/8.4):
# a workflow run has exactly one terminal outcome and the main agent is woken by
# a ``task-notification`` when it lands; there is no polling wait. GA keeps a
# ``workflow_terminal`` predicate, so it must resolve the run's real status.


WORKFLOW_RUN_ID_RE = re.compile(r"^wf_[0-9a-f]{32}$")

# ``degraded``/``partial`` are terminal: the run finished, some jobs failed.
WORKFLOW_TERMINAL_RUN_STATUSES = frozenset(
    {
        "succeeded",
        "completed",
        "degraded",
        "partial",
        "failed",
        "cancelled",
        "canceled",
        "killed",
        "interrupted",
        "closed",
        "stale",
    }
)

_WORKFLOW_RUN_TURN_STATUS = {
    "draft": "pending",
    "awaiting_approval": "pending",
    "queued": "pending",
    "pending": "pending",
    "running": "running",
    "succeeded": "completed",
    "completed": "completed",
    "closed": "completed",
    "cached": "completed",
    "degraded": "completed",
    "partial": "completed",
    "failed": "failed",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "killed": "killed",
    "interrupted": "interrupted",
    "stale": "stale",
}


def is_workflow_run_id(value):
    """True when ``value`` looks like a dynamic-workflow run id (``wf_`` + 32 hex)."""
    return bool(WORKFLOW_RUN_ID_RE.match(str(value or "").strip().lower()))


def workflow_run_is_terminal(status):
    return str(status or "").strip().lower() in WORKFLOW_TERMINAL_RUN_STATUSES


def workflow_run_turn_status(status):
    """Project a workflow run status onto the subagent ``turn_status`` vocabulary.

    ``degraded`` maps to ``completed`` on purpose: the run is finished and its
    partial result is authoritative. The raw run status travels separately as
    ``completion_status`` so a reader can still tell success from partial.
    """
    return _WORKFLOW_RUN_TURN_STATUS.get(str(status or "").strip().lower(), "running")

