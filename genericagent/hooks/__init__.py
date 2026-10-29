"""GenericAgent hooks runtime, modeled after Claude Code's hook protocol."""

from genericagent.hooks.models import (
    EVENT_NAMES,
    GUARD_EVENTS,
    HANDLER_TYPES,
    HookAggregate,
    HookEntry,
    HookHandler,
    HookPayload,
    HookResult,
)
from genericagent.hooks.config import parse_dynamic_handler
from genericagent.hooks.runtime import HookRuntime, hook_runtime_for_agent, run_hook_event

__all__ = [
    "EVENT_NAMES",
    "GUARD_EVENTS",
    "HANDLER_TYPES",
    "HookAggregate",
    "HookEntry",
    "HookHandler",
    "HookPayload",
    "HookResult",
    "HookRuntime",
    "parse_dynamic_handler",
    "hook_runtime_for_agent",
    "run_hook_event",
]
