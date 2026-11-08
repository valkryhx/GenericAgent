"""Hard capability boundaries for root and child agents.

Prompt text is useful guidance, but it is not an authorization boundary.  This
module keeps the boundary data-only so the same profile can be applied both to
the model-visible tool schema and to runtime dispatch.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable


ORCHESTRATION_TOOLS = frozenset(
    {
        "workflow",
        "spawn_agent",
        "list_agents",
        "wait_agent",
        "read_agent_result",
        "resume_agent",
        "send_message",
        "followup_task",
        "foreground_agent",
        "background_agent",
        "attach_agent",
        "detach_agent",
        "interrupt_agent",
        "close_agent",
        "task_create",
        "task_get",
        "task_update",
        "task_list",
        "team_status",
        "propose_goal",
    }
)
INTERNAL_SENTINEL_TOOLS = frozenset({"no_tool"})
# Tools a teammate gets back when team_mode is on: it may talk to peers/lead
# and use the shared task board, but never orchestrate (no spawn_agent/wait_agent).
TEAMMATE_TOOLS = frozenset(
    {
        "send_message",
        "task_create",
        "task_get",
        "task_update",
        "task_list",
        "team_status",
    }
)


def _normalize_tool_name(value) -> str:
    name = str(value or "").strip()
    if name.startswith("functions."):
        name = name[len("functions.") :]
    return name


@dataclass(frozen=True)
class SubagentCapabilityProfile:
    is_subagent: bool
    allowed_tools: frozenset[str]
    denied_tools: frozenset[str]
    allow_delegation: bool = False
    team_mode: bool = False

    def allows(self, tool_name: str) -> bool:
        name = _normalize_tool_name(tool_name)
        if name in INTERNAL_SENTINEL_TOOLS:
            return True
        if not name or name in self.denied_tools:
            return False
        return name in self.allowed_tools

    def to_dict(self) -> dict:
        return {
            "is_subagent": self.is_subagent,
            "allowed_tools": sorted(self.allowed_tools),
            "denied_tools": sorted(self.denied_tools),
            "allow_delegation": self.allow_delegation,
            "team_mode": self.team_mode,
        }


def build_subagent_capability_profile(
    *,
    is_subagent: bool,
    role_tools: Iterable[str] | None = None,
    allow_delegation: bool = False,
    team_mode: bool = False,
    available_tools: Iterable[str] | None = None,
    denied_tools: Iterable[str] | None = None,
) -> SubagentCapabilityProfile:
    """Build the tool surface for a child process.

    ``available_tools`` should be the names in the concrete schema for this process.
    A child gets every one of them except the orchestration tools (unless delegation is
    explicitly allowed); ``denied_tools`` can subtract further.

    ``role_tools`` is accepted for signature compatibility and is deliberately ignored.
    It used to be a whitelist, which made an unconstrained spawn get nothing -- see the
    comment below for why that is the wrong default. A role narrows *what the agent is
    told to do* through its system prompt, not *what it is able to do*; a boundary that
    the caller has to re-state on every spawn is one the caller will eventually forget.
    """
    available = {_normalize_tool_name(item) for item in (available_tools or ()) if _normalize_tool_name(item)}
    denied = {_normalize_tool_name(item) for item in (denied_tools or ()) if _normalize_tool_name(item)}
    # A subagent's default surface is everything the process has, minus orchestration.
    #
    # It used to be a whitelist built from ``role_tools``, which meant any spawn that did
    # not name tools got none. That is backwards from how the surface is actually chosen:
    # the interesting question is "which of my tools may this child use", and the answer
    # for an unconstrained spawn is "all of them". Four research subagents came back
    # blocked because their parent passed a role whose ``tools`` was empty, and an empty
    # list is what every builtin role and every role file that omits ``tools`` produces.
    #
    # ``role_tools`` is therefore no longer a whitelist. A caller that genuinely wants to
    # narrow the surface uses ``denied_tools`` (or the permission profile), which can only
    # ever subtract -- so a model that forgets to pass anything cannot accidentally strip
    # the child's ability to work. Capability is opt-out, never opt-in.
    allowed = set(available) if available else set()
    if is_subagent and not allow_delegation:
        allowed.difference_update(ORCHESTRATION_TOOLS)
        if team_mode:
            # A teammate keeps the collaboration surface (peer messaging + task board)
            # without the orchestration surface (it may not spawn or await agents).
            allowed.update(TEAMMATE_TOOLS)
    elif allow_delegation or not is_subagent:
        if available:
            allowed.update(available.intersection(ORCHESTRATION_TOOLS))
        else:
            allowed.update(ORCHESTRATION_TOOLS)
    allowed.update(INTERNAL_SENTINEL_TOOLS)
    allowed.difference_update(denied - INTERNAL_SENTINEL_TOOLS)
    return SubagentCapabilityProfile(
        is_subagent=bool(is_subagent),
        allowed_tools=frozenset(allowed),
        denied_tools=frozenset(denied - INTERNAL_SENTINEL_TOOLS),
        allow_delegation=bool(allow_delegation or not is_subagent),
        team_mode=bool(team_mode and is_subagent and not allow_delegation),
    )


def filter_tool_schema_for_capabilities(schema, profile: SubagentCapabilityProfile):
    """Return a fresh schema containing only tools granted by ``profile``."""
    filtered = []
    for item in schema or []:
        if not isinstance(item, dict):
            continue
        function = item.get("function") or {}
        name = function.get("name") if isinstance(function, dict) else None
        if profile.allows(name):
            filtered.append(item)
    return filtered
