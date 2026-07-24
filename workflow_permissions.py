from __future__ import annotations

from dataclasses import dataclass, field

from workflow_models import DEFAULT_PERMISSION_PROFILE


INHERIT_CURRENT_PERMISSIONS = DEFAULT_PERMISSION_PROFILE
READ_ONLY = "read_only"
RESTRICTED_MCP = "restricted_mcp"
EXPLICIT_APPROVAL = "explicit_approval"

DENIED_READ_ONLY_STATIC_TOOLS = frozenset({"file_write", "file_patch", "code_run", "web_execute_js"})
ALLOWED_READ_ONLY_STATIC_TOOLS = frozenset({"file_read", "web_scan", "no_tool", "ask_user", "load_skill"})
READ_ONLY_NAME_PREFIXES = ("read", "list", "get", "scan", "search", "show", "status", "inspect", "query", "fetch")
READ_ONLY_NAME_PARTS = ("_read", "_list", "_get", "_scan", "_search", "_show", "_status", "_query", "_fetch")


@dataclass(frozen=True)
class PermissionDecision:
    action: str
    reason: str
    profile: str = INHERIT_CURRENT_PERMISSIONS
    tool_name: str = ""
    details: dict = field(default_factory=dict)

    def __post_init__(self):
        if self.action not in {"allow", "deny", "ask"}:
            raise ValueError(f"invalid permission action: {self.action}")

    def to_dict(self) -> dict:
        return {
            "action": self.action,
            "reason": self.reason,
            "profile": self.profile,
            "toolName": self.tool_name,
            "details": dict(self.details),
        }


@dataclass(frozen=True)
class McpToolName:
    server: str
    tool: str


class ToolPermissionPolicy:
    def __init__(self, *, profile: str = INHERIT_CURRENT_PERMISSIONS, options: dict | None = None):
        self.profile = profile or INHERIT_CURRENT_PERMISSIONS
        self.options = dict(options or {})

    def evaluate(self, tool_name: str, args: dict | None = None) -> PermissionDecision:
        tool_name = str(tool_name or "")
        if self.profile == INHERIT_CURRENT_PERMISSIONS:
            return self._decision("allow", "inherit_current", tool_name)
        if self.profile == READ_ONLY:
            return self._evaluate_read_only(tool_name)
        if self.profile == RESTRICTED_MCP:
            return self._evaluate_restricted_mcp(tool_name)
        if self.profile == EXPLICIT_APPROVAL:
            return self._decision("ask", "explicit_approval_required", tool_name)
        return self._decision("deny", "unknown_permission_profile", tool_name)

    def _evaluate_read_only(self, tool_name: str) -> PermissionDecision:
        if is_mcp_tool_name(tool_name):
            parsed = parse_mcp_tool_name(tool_name)
            if _is_read_only_name(parsed.tool):
                return self._decision("allow", "read_only_mcp_read", tool_name, {"server": parsed.server, "mcpTool": parsed.tool})
            return self._decision("deny", "read_only_mcp_unknown", tool_name, {"server": parsed.server, "mcpTool": parsed.tool})
        if tool_name in DENIED_READ_ONLY_STATIC_TOOLS:
            return self._decision("deny", "read_only_static_write_or_execute", tool_name)
        if tool_name in ALLOWED_READ_ONLY_STATIC_TOOLS or _is_read_only_name(tool_name):
            return self._decision("allow", "read_only_static_safe", tool_name)
        return self._decision("deny", "read_only_static_unknown", tool_name)

    def _evaluate_restricted_mcp(self, tool_name: str) -> PermissionDecision:
        if not is_mcp_tool_name(tool_name):
            return self._decision("allow", "restricted_mcp_static_allowed", tool_name)
        parsed = parse_mcp_tool_name(tool_name)
        details = {"server": parsed.server, "mcpTool": parsed.tool}
        if tool_name in _option_set(self.options, "denied_mcp_tools"):
            return self._decision("deny", "restricted_mcp_tool_denied", tool_name, details)
        if parsed.server in _option_set(self.options, "denied_mcp_servers"):
            return self._decision("deny", "restricted_mcp_server_denied", tool_name, details)
        if tool_name in _option_set(self.options, "allowed_mcp_tools"):
            return self._decision("allow", "restricted_mcp_tool_allowed", tool_name, details)
        if parsed.server in _option_set(self.options, "allowed_mcp_servers"):
            return self._decision("allow", "restricted_mcp_server_allowed", tool_name, details)
        return self._decision("deny", "restricted_mcp_not_allowed", tool_name, details)

    def _decision(self, action: str, reason: str, tool_name: str, details: dict | None = None) -> PermissionDecision:
        return PermissionDecision(action=action, reason=reason, profile=self.profile, tool_name=tool_name, details=dict(details or {}))


def is_mcp_tool_name(tool_name: str) -> bool:
    return str(tool_name or "").startswith("mcp__")


def parse_mcp_tool_name(tool_name: str) -> McpToolName:
    parts = str(tool_name or "").split("__", 2)
    if len(parts) != 3 or parts[0] != "mcp":
        return McpToolName(server="", tool=str(tool_name or ""))
    return McpToolName(server=parts[1], tool=parts[2])


def build_permission_event(event_type: str, *, context: dict | None, tool_name: str, decision: PermissionDecision) -> dict:
    context = dict(context or {})
    return {
        "type": event_type,
        "runId": context.get("runId") or context.get("run_id"),
        "jobId": context.get("jobId") or context.get("job_id"),
        "toolName": tool_name,
        "profile": context.get("permissionProfile") or context.get("permission_profile") or decision.profile,
        "decision": decision.action,
        "reason": decision.reason,
        "permission": decision.to_dict(),
    }


def _option_set(options: dict, key: str) -> set[str]:
    value = options.get(key) or []
    if isinstance(value, str):
        value = [value]
    return {str(item) for item in value}


def _is_read_only_name(name: str) -> bool:
    lowered = str(name or "").lower().replace("-", "_")
    return lowered.startswith(READ_ONLY_NAME_PREFIXES) or any(part in lowered for part in READ_ONLY_NAME_PARTS)
