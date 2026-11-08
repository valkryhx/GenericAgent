"""内置工具的默认结果上限，对齐 Claude Code 319 的 ``maxResultSizeChars``。

**为什么单独一个模块**：`budget.py` 的 `_EXEMPT_TOOLS` 只决定「要不要走预算」，
而「每个工具允许多大」是个独立的事实。写进 `budget.py` 会让那个本来通用的
渲染器带上工具知识；写在 `handler.py` 又会让离线脚本（workflow child、
测试）拿不到。

上限值的来源是 `docs/20261107-CC内置工具实现调研.md`：

- §1.1 表格：Bash 是 `Pft()`（默认 30,000，上限 150,000）；Glob 100,000；
  Grep 20,000；Read / Write / Edit 用默认值。
- §10.1：Glob 与 Grep 的差异是**有意的**——「Glob 只回路径（短），Grep 回
  内容行（长）」。把两者设成同一个数，等于让只想要文件名的调用也背上内容行
  的预算。

**为什么 Read 用全局默认而不设专用值**：CC 的 Read 就是默认
`maxResultSizeChars`。给它一个更大的数只会让单次读取更容易撑爆上下文，
与设置预算的初衷相反。它现在受预算约束（不再豁免），全局 50,000 就是上限。
"""

from __future__ import annotations

from typing import Mapping

from genericagent.core.tool_results.limits import replace_tool_result_max_chars


# 工具名 → 字符上限。未列出的工具走全局默认（GA_TOOL_RESULT_MAX_CHARS / 50,000）。
TOOL_RESULT_DEFAULTS: Mapping[str, int] = {
    # Bash 对齐：默认 30,000。命令输出是「几十行正常、几万行就该落盘」的典型。
    "code_run": 30_000,
    # Glob 只回路径。
    "file_glob": 100_000,
    # Grep 回内容行，20,000 约等于 100 行上下文（CC 同样的取舍）。
    "file_grep": 20_000,
}


def install_tool_result_defaults() -> None:
    """把内置工具的默认上限注册进 limits 注册表。

    幂等，可以在每次构建 handler 时调用：`replace_tool_result_max_chars`
    只覆盖传入的键，MCP 运行时注册的 ``mcp__*`` 上限不受影响。
    """

    replace_tool_result_max_chars(TOOL_RESULT_DEFAULTS)
