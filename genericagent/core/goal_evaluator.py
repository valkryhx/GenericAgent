"""Goal evaluator wiring (Stage F, phase 1).

Registers a session-scoped ``Stop`` prompt hook (stage B) whose output is parsed
strictly against a ``{ok, reason, impossible?}`` schema (stage D). The Stop gate
(stage A) then blocks the turn end while the goal is unmet, exactly like CC's
blockable_turn_end goal hook. This module only builds/serializes; it never runs
the model itself, so it is unit-testable without network access.
"""

from __future__ import annotations

import json

# Session hook key; idempotent replacement so re-setting a goal never stacks.
GOAL_STOP_HOOK_KEY = "goal:stop"

# Strict evaluator schema (mirrors stage D's {ok,reason,impossible?} triple).
GOAL_OUTPUT_SCHEMA = {
    "type": "object",
    "required": ["ok", "reason"],
    "properties": {
        "ok": {"type": "boolean"},
        "reason": {"type": "string"},
        "impossible": {"type": "boolean"},
    },
}

GOAL_EVAL_PROMPT = """You are evaluating a stop-condition goal in GenericAgent. Read the conversation transcript in <hook_input>, then judge whether the goal condition is satisfied.

Your response MUST be a single JSON object matching this schema:
- {"ok": true, "reason": "<quote evidence from the transcript that satisfies the condition>"}
- {"ok": false, "reason": "<quote what is missing or what blocks the condition>"}
- {"ok": false, "impossible": true, "reason": "<explain why the condition can never be satisfied>"}

Always include "reason", quoting specific text whenever possible. If the transcript lacks clear evidence, return {"ok": false, "reason": "insufficient evidence in transcript"}. Only use "impossible" when the condition is genuinely unachievable in this session (self-contradictory, depends on an unavailable resource, or all reasonable approaches exhausted and independently confirmed). When in doubt, return {"ok": false} without "impossible". Do not wrap the JSON in prose or code fences.

GOAL CONDITION:
{condition}"""

DEFAULT_GOAL_TIMEOUT = 120.0
# Character budget for the serialized transcript handed to the evaluator.
DEFAULT_TRANSCRIPT_CHAR_BUDGET = 40000


def build_goal_prompt(condition: str) -> str:
    return GOAL_EVAL_PROMPT.replace("{condition}", str(condition or ""))


def build_goal_handler_spec(condition: str, *, timeout: float = DEFAULT_GOAL_TIMEOUT) -> dict:
    """The raw handler dict accepted by ``add_session_hook`` / parse_dynamic_handler."""
    return {
        "type": "prompt",
        "prompt": build_goal_prompt(condition),
        "outputSchema": GOAL_OUTPUT_SCHEMA,
        "timeout": float(timeout),
        # One reinforced retry on invalid JSON (weak models occasionally wrap
        # the object in prose). Scoped to the goal handler only.
        "retryOnInvalidOutput": True,
        # A broken/timeout evaluator must never trap the turn (Stop is in
        # ON_FAILURE_IGNORED_EVENTS, but be explicit for readability).
        "onFailure": "continue",
    }


def _message_to_text(message) -> str:
    if not isinstance(message, dict):
        return str(message)
    role = str(message.get("role") or "?")
    content = message.get("content")
    if isinstance(content, str):
        body = content
    elif isinstance(content, list):
        parts = []
        for block in content:
            if isinstance(block, dict):
                if block.get("type") == "text":
                    parts.append(str(block.get("text") or ""))
                elif block.get("type") == "tool_use":
                    parts.append("[tool_use %s]" % block.get("name"))
                elif block.get("type") == "tool_result":
                    parts.append("[tool_result]")
            else:
                parts.append(str(block))
        body = " ".join(p for p in parts if p)
    else:
        body = str(content) if content is not None else ""
    return "%s: %s" % (role, body)


def serialize_transcript(history, *, char_budget: int = DEFAULT_TRANSCRIPT_CHAR_BUDGET) -> str:
    """Turn a backend history (list of messages) or string list into text.

    Truncates from the front (oldest first) when over budget, prefixing a note
    so the evaluator knows earlier context was dropped (CC m0459 UYr ~0.5x
    context budget; here a plain char budget is enough for phase 1).
    """
    if not history:
        return ""
    if isinstance(history, str):
        lines = [history]
    else:
        lines = [_message_to_text(m) for m in history]
    text = "\n".join(line for line in lines if line)
    if len(text) <= char_budget:
        return text
    omitted = len(text) - char_budget
    tail = text[-char_budget:]
    note = (
        "[Earlier conversation truncated to fit the evaluator's context window - "
        "%d characters omitted. Evaluate against the recent transcript; if the "
        "required evidence may be in the omitted prefix, return "
        "{\"ok\": false, \"reason\": \"insufficient evidence in transcript\"}.]\n"
    ) % omitted
    return note + tail


def transcript_from_agent(agent, *, char_budget: int = DEFAULT_TRANSCRIPT_CHAR_BUDGET) -> str:
    """Best-effort transcript text for the evaluator payload.

    Prefers the backend history (full conversation for the session). Falls back
    to the app-level ``history`` list of bracketed lines.
    """
    history = None
    backend = getattr(getattr(agent, "llmclient", None), "backend", None)
    if backend is not None:
        history = getattr(backend, "history", None)
    if not history:
        history = getattr(agent, "history", None)
    return serialize_transcript(history, char_budget=char_budget)


def register_goal_hook(agent, condition: str, *, timeout: float = DEFAULT_GOAL_TIMEOUT):
    """Install (or replace) the goal Stop hook on the agent's hook runtime."""
    from genericagent.hooks.runtime import hook_runtime_for_agent
    runtime = hook_runtime_for_agent(agent)
    return runtime.add_session_hook(
        "Stop",
        build_goal_handler_spec(condition, timeout=timeout),
        key=GOAL_STOP_HOOK_KEY,
        emit=getattr(agent, "hook_emit", None),
    )


def unregister_goal_hook(agent) -> bool:
    from genericagent.hooks.runtime import hook_runtime_for_agent
    runtime = getattr(agent, "hook_runtime", None)
    if runtime is None:
        return False
    try:
        return runtime.remove_session_hook(GOAL_STOP_HOOK_KEY)
    except Exception:
        return False


def goal_payload_extra(agent) -> dict:
    """Extra Stop payload fields the goal evaluator needs (transcript text)."""
    return {"goal_transcript": transcript_from_agent(agent)}


# --- Stage 2-A: defer goal evaluation while background sub-agents run --------
#
# CC defers goal evaluation when background agents/shells are still producing
# (m0671:3304-3348): evaluating "is the goal met?" before the background work
# lands would misjudge it and wrongly block the turn. GA's async sub-agents are
# tracked by SubagentManager; a descendant is "in flight" when its process is
# alive and its turn is pending/running.

_IN_FLIGHT_TURN_STATUSES = frozenset({"pending", "running"})
_TERMINAL_PROCESS_STATUSES = frozenset({"exited", "shutdown", "killed"})


def is_in_flight_agent_state(state) -> bool:
    """Whether one sub-agent state means 'still working'."""
    process_status = str(getattr(state, "process_status", "") or "").strip().lower()
    if process_status in _TERMINAL_PROCESS_STATUSES:
        return False
    turn_status = str(getattr(state, "turn_status", "") or "").strip().lower()
    return turn_status in _IN_FLIGHT_TURN_STATUSES or process_status == "alive"


def _manager_for(agent, handler):
    """Resolve a SubagentManager from the handler, falling back to the agent.

    In production the manager factory lives on GenericAgentHandler (it needs the
    per-session ``session_data_dir`` and team identity), while ``run_stop_gate``
    has the handler at hand but ``_hook_agent()`` only yields the GenericAgent.
    So the handler is the primary source; the agent fallback keeps unit tests
    that attach the factory to a fake agent working.
    """
    for owner in (handler, agent):
        if owner is None:
            continue
        getter = getattr(owner, "_get_subagent_manager", None)
        if getter is None:
            continue
        manager = getter()
        if manager is not None:
            return manager
    return None


def list_in_flight_task_labels(agent, handler=None) -> list:
    """Human labels for in-flight background sub-agents (2-B check-in copy).

    Returns short ``"<task_name> · <type>"`` strings, never raises. Empty
    list when there is no manager / session or nothing is in flight.
    """
    session_id = str(getattr(agent, "session_id", "") or "").strip()
    if not session_id and handler is not None:
        session_id = str(getattr(getattr(handler, "parent", None), "session_id", "") or "").strip()
    if not session_id:
        return []
    try:
        manager = _manager_for(agent, handler)
        if manager is None:
            return []
        states = manager.list_descendants(session_id)
    except Exception:
        return []
    labels = []
    for s in states:
        try:
            if not is_in_flight_agent_state(s):
                continue
            name = str(getattr(s, "task_name", "") or getattr(s, "agent_id", "") or "subagent")
            typ = str(getattr(s, "agent_type", "") or getattr(s, "role", "") or "agent")
            labels.append(f"{name} · {typ}")
        except Exception:
            continue
    return labels

def has_in_flight_agents(agent, handler=None) -> bool:
    """True when this session has at least one in-flight background sub-agent.

    Best-effort: any failure to reach the manager is treated as 'not in flight'
    so a broken query never silently defers goal evaluation forever, and a bug
    here can never trap the user's turn open.

    ``handler`` is optional for backward compatibility and for unit tests; the
    real caller in ``run_stop_gate`` always passes it because that is where the
    sub-agent manager factory (and the authoritative session id) live.
    """
    session_id = str(getattr(agent, "session_id", "") or "").strip()
    if not session_id and handler is not None:
        session_id = str(getattr(getattr(handler, "parent", None), "session_id", "") or "").strip()
    if not session_id:
        return False
    try:
        manager = _manager_for(agent, handler)
        if manager is None:
            return False
        states = manager.list_descendants(session_id)
    except Exception:
        return False
    return any(is_in_flight_agent_state(s) for s in states)
