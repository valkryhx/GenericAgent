"""Hook domain models shared by config, execution, and runtime aggregation."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# The event names follow Claude Code's public hook event surface. GA only emits
# the events wired in core/runtime today; keeping the complete vocabulary makes
# configuration validation and future event integration deterministic.
# Claude Code 2.1.303 m0062_0xe4ee087.js:501. Keep this list byte-for-byte
# aligned with CC; GA's integration status is tracked separately in hook_status().
EVENT_NAMES = frozenset({
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "PostToolBatch",
    "Notification",
    "UserPromptSubmit",
    "UserPromptExpansion",
    "SessionStart",
    "SessionEnd",
    "Stop",
    "StopFailure",
    "SubagentStart",
    "SubagentStop",
    "PreCompact",
    "PostCompact",
    "PreModelSwitch",
    "PostModelSwitch",
    "PermissionRequest",
    "PermissionDenied",
    "Setup",
    "TeammateIdle",
    "TaskCreated",
    "TaskCompleted",
    "Elicitation",
    "ElicitationResult",
    "ConfigChange",
    "WorktreeCreate",
    "WorktreeRemove",
    "InstructionsLoaded",
    "CwdChanged",
    "FileChanged",
    "DirectoryAdded",
    "MessageDisplay",
})

HANDLER_TYPES = frozenset({"command", "prompt", "agent", "http", "mcp_tool"})
GUARD_EVENTS = frozenset({
    "UserPromptSubmit",
    "PreToolUse",
    "PermissionRequest",
})
STOP_LIKE_EVENTS = frozenset({"Stop", "SubagentStop", "StopFailure"})


@dataclass(frozen=True)
class HookHandler:
    kind: str
    options: dict[str, Any] = field(default_factory=dict)

    @property
    def timeout(self) -> float:
        return float(self.options.get("timeout") or 10.0)

    @property
    def on_failure(self) -> str:
        value = str(self.options.get("onFailure") or "continue").strip().lower()
        return "block" if value == "block" else "continue"

    @property
    def once(self) -> bool:
        return bool(self.options.get("once", False))

    @property
    def status_message(self) -> str:
        return str(self.options.get("statusMessage") or "")

    @property
    def async_requested(self) -> bool:
        return bool(self.options.get("async", False) or self.options.get("asyncRewake", False))


@dataclass(frozen=True)
class HookEntry:
    event: str
    matcher: str | None
    handlers: tuple[HookHandler, ...]
    source: str
    source_path: str = ""
    source_rank: int = 0
    entry_index: int = 0


@dataclass
class HookPayload:
    event: str
    data: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        payload = dict(self.data)
        payload["hook_event_name"] = self.event
        return payload


@dataclass
class HookResult:
    event: str
    handler: HookHandler
    source: str
    source_path: str
    hook_run_id: str
    status: str
    duration_ms: int = 0
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    output: dict[str, Any] = field(default_factory=dict)
    error: str = ""
    blocking: bool = False

    @property
    def output_dict(self) -> dict[str, Any]:
        return self.output if isinstance(self.output, dict) else {}

    @property
    def hook_specific(self) -> dict[str, Any]:
        value = self.output_dict.get("hookSpecificOutput")
        return value if isinstance(value, dict) else {}

    @property
    def permission_decision(self) -> str:
        value = self.hook_specific.get("permissionDecision")
        if value in ("allow", "ask", "deny"):
            return str(value)
        return ""

    @property
    def additional_context(self) -> str:
        value = self.hook_specific.get("additionalContext")
        if value is None:
            # Plain-text command output is carried as a GA extension; CC shows
            # it as hook output while JSON controls remain in hookSpecificOutput.
            value = self.output_dict.get("additionalContext")
        return str(value or "").strip()

    @property
    def system_message(self) -> str:
        return str(self.output_dict.get("systemMessage") or "").strip()

    def to_audit_dict(self) -> dict[str, Any]:
        return {
            "type": "hook_executed",
            "hook_run_id": self.hook_run_id,
            "event": self.event,
            "source": self.source,
            "source_path": self.source_path,
            "handler_type": self.handler.kind,
            "status": self.status,
            "duration_ms": self.duration_ms,
            "exit_code": self.exit_code,
            "blocking": self.blocking,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "error": self.error,
            "output": self.output,
        }


@dataclass
class HookAggregate:
    event: str
    results: list[HookResult] = field(default_factory=list)
    additional_context: list[str] = field(default_factory=list)
    system_messages: list[str] = field(default_factory=list)
    permission_decision: str = ""
    decision: str = ""
    reason: str = ""
    updated_input: dict[str, Any] | None = None
    updated_output: Any = None
    updated_output_set: bool = False
    continue_processing: bool = True
    prevent_continuation: bool = False

    @property
    def context_text(self) -> str:
        return "\n".join(item for item in self.additional_context if item)

    @property
    def system_text(self) -> str:
        return "\n".join(dict.fromkeys(item for item in self.system_messages if item))

    @property
    def blocked(self) -> bool:
        return (
            not self.continue_processing
            or self.decision == "block"
            or self.permission_decision == "deny"
            or self.prevent_continuation
        )

    def add(self, result: HookResult) -> None:
        self.results.append(result)
        output = result.output_dict
        if output.get("continue") is False:
            self.continue_processing = False
        decision = str(output.get("decision") or "").strip().lower()
        if decision:
            self.decision = decision
        if output.get("reason"):
            self.reason = str(output.get("reason") or "")
        specific = result.hook_specific
        if result.additional_context:
            self.additional_context.append(result.additional_context)
        if result.system_message:
            self.system_messages.append(result.system_message)
        # Permission decisions are combined with deny > ask > allow.
        rank = {"": 0, "allow": 1, "ask": 2, "deny": 3}
        if rank.get(result.permission_decision, 0) > rank.get(self.permission_decision, 0):
            self.permission_decision = result.permission_decision
        updated = specific.get("updatedInput")
        if isinstance(updated, dict):
            self.updated_input = dict(updated)
        # CC 303 m0120:7129 uses updatedToolOutput. updatedOutput is retained
        # only as a GA legacy alias; prefer the CC field when both are present.
        if "updatedToolOutput" in specific:
            self.updated_output = specific.get("updatedToolOutput")
            self.updated_output_set = True
        elif "updatedOutput" in specific:
            self.updated_output = specific.get("updatedOutput")
            self.updated_output_set = True
        if output.get("preventContinuation") is True:
            self.prevent_continuation = True
