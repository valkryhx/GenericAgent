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
# Guard events whose *execution failure* is fail-closed (CC 303 Ilt() =
# m0459:158274; includes UserPromptSubmit etc. minus PostToolBatch/TaskCreated/
# PreCompact). Unset onFailure on these blocks; explicit onFailure:continue opts out.
GUARD_EVENTS = frozenset({
    "UserPromptSubmit",
    "PreToolUse",
    "PermissionRequest",
})
# Guard events whose *config-load error* is fail-closed (CC 303 T4 = m0062:2815).
# A guard that cannot be loaded may be the permission gate itself, so it blocks
# unconditionally (no per-hook opt-out - there is no handler to carry onFailure).
CONFIG_GUARD_EVENTS = frozenset({"PreToolUse", "PermissionRequest"})
STOP_LIKE_EVENTS = frozenset({"Stop", "SubagentStop", "StopFailure"})


@dataclass(frozen=True)
class HookHandler:
    kind: str
    options: dict[str, Any] = field(default_factory=dict)

    @property
    def timeout(self) -> float:
        # CC's config-based async command fallback is 600s (m0459:157745),
        # not the 10s default used by ordinary notification hooks.
        if self.kind == "command" and self.async_requested:
            return float(self.options.get("timeout") or 600.0)
        return float(self.options.get("timeout") or 10.0)

    @property
    def on_failure(self) -> str:
        value = str(self.options.get("onFailure") or "continue").strip().lower()
        return "block" if value == "block" else "continue"

    @property
    def on_failure_explicit(self) -> str | None:
        """Raw onFailure policy, or None when the author did not set it.

        None matters for guard events: an unset onFailure defaults to
        fail-closed, while an explicit ``continue`` opts out.
        """
        raw = self.options.get("onFailure")
        if raw is None:
            return None
        value = str(raw).strip().lower()
        return value if value in ("block", "continue") else None

    @property
    def output_schema(self) -> dict[str, Any] | None:
        """Optional JSON schema for a prompt/agent hook's model output.

        When set, the executor parses the model reply strictly against it
        (no plain-text fallback). Used by the goal evaluator triple
        ``{ok, reason, impossible?}`` (CC 303 m0459:154361-154451).
        """
        value = self.options.get("outputSchema")
        if value is None:
            value = self.options.get("output_schema")
        return value if isinstance(value, dict) and value else None

    @property
    def retry_on_invalid_output(self) -> bool:
        """One extra attempt when a structured reply fails strict parsing.

        Opt-in per handler (``retryOnInvalidOutput``); only the goal evaluator
        sets it. Ordinary hooks keep single-attempt semantics.
        """
        return bool(self.options.get("retryOnInvalidOutput", False))

    @property
    def once(self) -> bool:
        return bool(self.options.get("once", False))

    @property
    def status_message(self) -> str:
        return str(self.options.get("statusMessage") or "")

    @property
    def async_requested(self) -> bool:
        return self.kind == "command" and bool(
            self.options.get("async", False) or self.options.get("asyncRewake", False)
        )

    @property
    def async_rewake(self) -> bool:
        return self.kind == "command" and bool(self.options.get("asyncRewake", False))

    @property
    def rewake_message(self) -> str:
        return str(self.options.get("rewakeMessage") or "").strip()

    @property
    def rewake_summary(self) -> str:
        return str(self.options.get("rewakeSummary") or "Stop hook feedback").strip()


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
    backgrounded: bool = False

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
    def permission_behavior(self) -> str:
        # PermissionRequest stores its verdict in a nested decision object:
        # hookSpecificOutput.decision.behavior. PreToolUse keeps using
        # hookSpecificOutput.permissionDecision.
        decision = self.hook_specific.get("decision")
        if isinstance(decision, dict) and decision.get("behavior") in {"allow", "deny"}:
            return str(decision["behavior"])
        return ""

    @property
    def permission_message(self) -> str:
        decision = self.hook_specific.get("decision")
        if isinstance(decision, dict):
            return str(decision.get("message") or "").strip()
        return ""

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
            "backgrounded": self.backgrounded,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "error": self.error,
            "output": self.output,
        }


@dataclass(frozen=True)
class HookWakeRequest:
    """A local asyncRewake request ready to be injected into the next turn."""

    event: str
    hook_run_id: str
    hook_name: str
    message: str
    summary: str
    stdout: str = ""
    stderr: str = ""
    exit_code: int | None = None
    session_id: str = ""
    agent_id: str = ""


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
        return chr(10).join(dict.fromkeys(item for item in self.system_messages if item))

    @property
    def permission_behavior(self) -> str:
        # HookAggregate is what the tool dispatcher consumes. Keep this
        # property aligned with the strongest PermissionRequest verdict.
        if self.permission_decision in {"allow", "deny"}:
            return self.permission_decision
        for result in reversed(self.results):
            if result.permission_behavior in {"allow", "deny"}:
                return result.permission_behavior
        return ""

    @property
    def permission_message(self) -> str:
        for result in reversed(self.results):
            if result.permission_message:
                return result.permission_message
        return ""

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
        # PermissionRequest nests updatedInput under decision, unlike
        # PreToolUse. Later hook results win, matching CC's aggregation order.
        nested_decision = specific.get("decision")
        if isinstance(nested_decision, dict) and isinstance(nested_decision.get("updatedInput"), dict):
            self.updated_input = dict(nested_decision["updatedInput"])
        if isinstance(nested_decision, dict) and nested_decision.get("behavior") in {"allow", "deny"}:
            self.permission_decision = str(nested_decision["behavior"])
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
        # Evaluator-style structured decision {ok, reason, impossible?}
        # (CC 303 m0459:154463-154490). ok=false is a block; impossible=true
        # is a non-blocking terminal signal (goal clears without continuing).
        ok = output.get("ok")
        evaluator_hook = result.handler.kind in ("prompt", "agent") and result.handler.output_schema
        if evaluator_hook and isinstance(ok, bool) and ("reason" in output or "impossible" in output):
            reason = str(output.get("reason") or "")
            if ok:
                if reason:
                    self.reason = reason
            elif output.get("impossible") is True:
                if reason:
                    self.reason = reason
            else:
                self.decision = "block"
                if reason:
                    self.reason = reason
