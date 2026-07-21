"""主 agent 三档权限引擎：read_only / ask / full_access。

对标 Codex 三档预设（Read Only / Ask for approval / Full Access），但**不引入
OS sandbox**：能力边界 = 工具读/写分类 + allow|ask|deny，而非内核沙箱。

- read_only    只读类工具 allow；写/执行/有副作用工具 deny（结果回模型说明当前只读）
- ask          只读 allow；写/执行需审批（回 ask，前端弹窗，未接则等同拒绝）
- full_access  全部 allow（默认档，兼容 GA 今天「工具全开」行为）

工具读/写分类复用 workflow_permissions 已有的判定，保证主 agent 与 workflow 子
agent 对同一工具的读/写认知一致。决策对象也复用 workflow_permissions.PermissionDecision，
让 dispatch 钩子与事件结构统一。
"""

from __future__ import annotations

from workflow_permissions import (
    DENIED_READ_ONLY_STATIC_TOOLS,
    PermissionDecision,
    is_mcp_tool_name,
    parse_mcp_tool_name,
    _is_read_only_name,
)


READ_ONLY = "read_only"
ASK = "ask"
FULL_ACCESS = "full_access"

PERMISSION_MODES = (READ_ONLY, ASK, FULL_ACCESS)
DEFAULT_PERMISSION_MODE = FULL_ACCESS

# 与 workflow_permissions.ALLOWED_READ_ONLY_STATIC_TOOLS 同源的静态只读白名单。
# 这些工具即便名字不带 read/list 前缀也应归为读类。
_STATIC_READ_TOOLS = frozenset(
    {"file_read", "web_scan", "no_tool", "ask_user", "load_skill"}
)


def normalize_permission_mode(mode: object) -> str:
    """把任意输入收敛到三档之一；未知/空值回落默认档 full_access。"""
    text = str(mode or "").strip()
    return text if text in PERMISSION_MODES else DEFAULT_PERMISSION_MODE


def classify_tool(tool_name: str) -> tuple[str, str]:
    """把工具分类为 ('read'|'mutating', reason)。

    未知静态工具按 mutating 处理（保守：只读档下宁可拒也不误放行）。
    """
    tool_name = str(tool_name or "")
    if is_mcp_tool_name(tool_name):
        parsed = parse_mcp_tool_name(tool_name)
        if _is_read_only_name(parsed.tool):
            return "read", "mcp_read_name"
        return "mutating", "mcp_non_read"
    if tool_name in DENIED_READ_ONLY_STATIC_TOOLS:
        return "mutating", "static_write_or_execute"
    if tool_name in _STATIC_READ_TOOLS or _is_read_only_name(tool_name):
        return "read", "static_read"
    return "mutating", "static_unknown"


class PermissionModePolicy:
    """主 agent 权限档策略。evaluate 返回 allow/deny/ask 决策。"""

    def __init__(self, mode: str = DEFAULT_PERMISSION_MODE):
        self.mode = normalize_permission_mode(mode)

    def evaluate(self, tool_name: str, args: dict | None = None) -> PermissionDecision:
        tool_name = str(tool_name or "")
        if self.mode == FULL_ACCESS:
            return self._decision("allow", "full_access", tool_name)
        kind, class_reason = classify_tool(tool_name)
        if kind == "read":
            return self._decision("allow", f"read_allowed:{class_reason}", tool_name)
        # 写/执行/未知：只读档拒绝，ask 档要审批
        if self.mode == READ_ONLY:
            return self._decision("deny", f"read_only_blocked:{class_reason}", tool_name)
        return self._decision("ask", f"approval_required:{class_reason}", tool_name)

    def _decision(self, action: str, reason: str, tool_name: str) -> PermissionDecision:
        return PermissionDecision(
            action=action, reason=reason, profile=self.mode, tool_name=tool_name
        )


def build_permission_mode_policy(mode: object) -> PermissionModePolicy:
    """从任意输入构造策略；未知档回落默认档。"""
    return PermissionModePolicy(normalize_permission_mode(mode))
