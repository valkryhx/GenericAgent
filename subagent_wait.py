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
    )
