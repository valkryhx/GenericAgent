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
    }
)
INTERNAL_SENTINEL_TOOLS = frozenset({"no_tool"})


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
        }


def build_subagent_capability_profile(
    *,
    is_subagent: bool,
    role_tools: Iterable[str] | None = None,
    allow_delegation: bool = False,
    available_tools: Iterable[str] | None = None,
    denied_tools: Iterable[str] | None = None,
) -> SubagentCapabilityProfile:
    """Build a closed-world capability set.

    ``available_tools`` should be the names in the concrete schema for this
    process.  A generic child gets every known non-orchestration tool from that
    schema; a role with ``role_tools`` gets only its explicit list.  Unknown
    names are never implicitly granted.
    """
    available = {_normalize_tool_name(item) for item in (available_tools or ()) if _normalize_tool_name(item)}
    role = None if role_tools is None else {
        _normalize_tool_name(item) for item in role_tools if _normalize_tool_name(item)
    }
    denied = {_normalize_tool_name(item) for item in (denied_tools or ()) if _normalize_tool_name(item)}

    if role is None:
        allowed = set(available)
    else:
        allowed = set(role)
    if is_subagent and not allow_delegation:
        allowed.difference_update(ORCHESTRATION_TOOLS)
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
