"""结构化提问（ask_user_question）的准入、运行时与协议。

与旧的 `ask_user` 的区别（见实施方案 §0）：

- `ask_user`：硬中断，`should_exit=True`，答案在下一条用户消息里回来，
  模型下一轮重新拿上下文。
- `ask_user_question`：在工具调用内部阻塞，答案作为本次 tool_result 回来，
  模型在同一轮里就能继续。

模块划分：

- `admit`   —— 答案准入（只能回答，不能改题）
- `runtime` —— 阻塞等待与消息协议（对标 `permissions/runtime.py`）
"""

from __future__ import annotations

from typing import Any

# 一次提问最多几道题。CC 的 inputSchema 有数量上限（调研 §1.2）；
# GA 取同样的量级，避免一次塞进几十道题把卡片撑爆。
MAX_QUESTIONS = 4

# 无论权限档位如何，都必须走「ask」的工具。
#
# 为什么需要这个特例（实施方案 §4.5）：GA 默认档是 `full_access`，
# 它对所有工具直接 allow。`ask_user_question` 的价值就在于**弹出卡片并阻塞**——
# 一旦被 allow 放行，`do_ask_user_question` 就永远不会走到审批路径，
# 卡片永远不弹，工具静默失效。
#
# CC 用 `requiresUserInteraction()` 达到同样效果（调研 §2.1），
# 那是工具声明的一部分；GA 的权限决策集中在 policy 层，所以特例也放在那里。
ALWAYS_ASK_TOOLS = frozenset({"ask_user_question"})


def hide_submit_tab(questions: list[dict[str, Any]]) -> bool:
    """是否隐藏「确认页」页签。

    对齐 CC 的 `hideSubmitTab`（`full:69153`，调研 §9.7.2）：
    **恰好 1 题且非多选**时才隐藏 —— 此时选中即提交，不需要确认页。

    ⚠️ 这与「要不要自动提交」是**两套独立判断**：
    这里看的是 schema 的 `multiSelect` 字段，自动提交看的是**运行时答案形状**
    （是不是列表）。两者在「单题单选」时恰好同时为真，但不能合并成一个开关。
    """
    if len(questions) != 1:
        return False
    return not bool(questions[0].get("multiSelect", False))


def validate_questions(questions: Any) -> str:
    """校验题目结构。返回空串表示合法，否则返回拒绝原因。"""
    if not isinstance(questions, list) or not questions:
        return "questions must be a non-empty list"

    if len(questions) > MAX_QUESTIONS:
        return "at most %d questions per call, got %d" % (MAX_QUESTIONS, len(questions))

    seen: set[str] = set()
    for idx, q in enumerate(questions):
        if not isinstance(q, dict):
            return "question %d must be an object" % idx
        key = str(q.get("key") or "").strip()
        if not key:
            return "question %d is missing a non-empty key" % idx
        if key in seen:
            return "duplicate question key: %s" % key
        seen.add(key)
        if not str(q.get("question") or "").strip():
            return "question %s is missing its text" % key
        options = q.get("options", [])
        if options is not None and not isinstance(options, list):
            return "options for %s must be a list" % key

    return ""

