from __future__ import annotations

import json
from pathlib import Path

from subagent_state import read_json_or_none
from workflow_permissions import (
    EXPLICIT_APPROVAL,
    INHERIT_CURRENT_PERMISSIONS,
    READ_ONLY,
    RESTRICTED_MCP,
    PermissionDecision,
    ToolPermissionPolicy,
)

_PERMISSION_PROFILES = {
    INHERIT_CURRENT_PERMISSIONS,
    READ_ONLY,
    RESTRICTED_MCP,
    EXPLICIT_APPROVAL,
}
_OPTION_KEYS = (
    "allowed_tools",
    "denied_tools",
    "allowed_mcp_servers",
    "denied_mcp_servers",
    "allowed_mcp_tools",
    "denied_mcp_tools",
)
_PARENT_PERMISSION_MODES = {"read_only", "ask", "full_access"}


class SubagentPermissionPolicy:
    def __init__(self, profile=INHERIT_CURRENT_PERMISSIONS, options=None, parent_permission_mode=None):
        metadata = normalize_permission_metadata(
            {
                "permission_profile": profile,
                "permission_options": dict(options or {}),
                "parent_permission_mode": parent_permission_mode,
            }
        )
        self.profile = metadata["permission_profile"]
        self.options = metadata["options"]
        self.parent_permission_mode = metadata.get("parent_permission_mode")
        self._delegate = ToolPermissionPolicy(profile=self.profile, options=self.options)

    def evaluate(self, tool_name, args=None):
        tool_name = str(tool_name or "")
        denied_tools = set(self.options.get("denied_tools") or [])
        allowed_tools = set(self.options.get("allowed_tools") or [])
        if tool_name in denied_tools:
            return PermissionDecision(
                action="deny",
                reason="subagent_tool_denied",
                profile=self.profile,
                tool_name=tool_name,
            )
        if allowed_tools and not tool_name.startswith("mcp__") and tool_name not in allowed_tools:
            return PermissionDecision(
                action="deny",
                reason="subagent_tool_not_allowed",
                profile=self.profile,
                tool_name=tool_name,
            )
        if self.profile == INHERIT_CURRENT_PERMISSIONS and self.parent_permission_mode:
            from permission_policy import build_permission_mode_policy

            inherited = build_permission_mode_policy(self.parent_permission_mode).evaluate(tool_name, args or {})
            return PermissionDecision(
                action=inherited.action,
                reason=f"inherit_current:{inherited.reason}",
                profile=self.profile,
                tool_name=tool_name,
                details={
                    "parent_permission_mode": self.parent_permission_mode,
                    "effective_profile": inherited.profile,
                },
            )
        return self._delegate.evaluate(tool_name, args or {})


def normalize_permission_metadata(raw=None):
    raw = dict(raw or {})
    profile = str(
        raw.get("permission_profile")
        or raw.get("permissionProfile")
        or raw.get("profile")
        or INHERIT_CURRENT_PERMISSIONS
    ).strip()
    if profile not in _PERMISSION_PROFILES:
        profile = INHERIT_CURRENT_PERMISSIONS
    options = {}
    raw_options = raw.get("permission_options") or raw.get("permissionOptions") or raw.get("options") or {}
    if isinstance(raw_options, str):
        try:
            raw_options = json.loads(raw_options)
        except json.JSONDecodeError:
            raw_options = {}
    if isinstance(raw_options, dict):
        raw = {**raw, **raw_options}
    for key in _OPTION_KEYS:
        values = raw.get(key)
        if values is None:
            continue
        if isinstance(values, str):
            values = [values]
        options[key] = [str(item) for item in values if str(item).strip()]
    parent_permission_mode = str(
        raw.get("parent_permission_mode")
        or raw.get("parentPermissionMode")
        or ""
    ).strip()
    if parent_permission_mode not in _PARENT_PERMISSION_MODES:
        parent_permission_mode = None
    return {
        "permission_profile": profile,
        "options": options,
        "parent_permission_mode": parent_permission_mode,
    }


def build_subagent_permission_policy(raw=None):
    metadata = normalize_permission_metadata(raw)
    return SubagentPermissionPolicy(
        metadata["permission_profile"],
        metadata["options"],
        parent_permission_mode=metadata.get("parent_permission_mode"),
    )


def load_subagent_permission_policy(task_dir):
    state = read_json_or_none(Path(task_dir) / "state.json") or {}
    metadata = normalize_permission_metadata(state)
    if (
        metadata["permission_profile"] == INHERIT_CURRENT_PERMISSIONS
        and not metadata["options"]
        and not metadata.get("parent_permission_mode")
    ):
        return None
    return build_subagent_permission_policy(metadata)
