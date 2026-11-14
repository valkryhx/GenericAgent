"""ask_user_question 在权限层的特殊地位。

它必须**任何档位都走 ask**，原因（实施方案 §4.5）：
GA 默认档是 `full_access`，而 `full_access` 对所有工具直接 allow。
如果 `ask_user_question` 也被 allow，权限层就直接放行，
`do_ask_user_question` 根本不会被调用到审批路径，卡片永远不弹。

CC 用 `requiresUserInteraction()` 达到同样效果（调研 §2.1 / §2.3）。

另一个要点：它仍然要出现在 `_STATIC_READ_TOOLS` 里。
那不是为了「放行」，是为了 `classify_tool` 把它归为 read ——
提问不改变任何状态，不该被只读档当成写操作拒掉。
"""

from __future__ import annotations

import unittest

from genericagent.core.permissions.policy import (
    ASK,
    FULL_ACCESS,
    READ_ONLY,
    PermissionModePolicy,
    classify_tool,
)
from genericagent.core.ask_user_question import ALWAYS_ASK_TOOLS


class AlwaysAskTest(unittest.TestCase):
    def test_ask_user_question_asks_in_full_access(self):
        policy = PermissionModePolicy(FULL_ACCESS)
        decision = policy.evaluate("ask_user_question", {})
        self.assertEqual(ASK, decision.action)

    def test_ask_user_question_asks_in_read_only(self):
        policy = PermissionModePolicy(READ_ONLY)
        decision = policy.evaluate("ask_user_question", {})
        self.assertEqual(ASK, decision.action)

    def test_ask_user_question_asks_in_ask_mode(self):
        policy = PermissionModePolicy(ASK)
        decision = policy.evaluate("ask_user_question", {})
        self.assertEqual(ASK, decision.action)

    def test_other_tools_unaffected_in_full_access(self):
        """不能因为加了特例就把 full_access 的整体行为改了。"""
        policy = PermissionModePolicy(FULL_ACCESS)
        self.assertEqual("allow", policy.evaluate("file_write", {}).action)
        self.assertEqual("allow", policy.evaluate("code_run", {}).action)

    def test_old_ask_user_still_full_access_allow(self):
        """旧工具行为不变 —— 不能顺手改它。"""
        policy = PermissionModePolicy(FULL_ACCESS)
        self.assertEqual("allow", policy.evaluate("ask_user", {}).action)


class ClassificationTest(unittest.TestCase):
    def test_ask_user_question_classified_as_read(self):
        kind, _ = classify_tool("ask_user_question")
        self.assertEqual("read", kind)

    def test_always_ask_set_contains_only_the_new_tool(self):
        self.assertIn("ask_user_question", ALWAYS_ASK_TOOLS)
        self.assertNotIn("ask_user", ALWAYS_ASK_TOOLS)


if __name__ == "__main__":
    unittest.main()
