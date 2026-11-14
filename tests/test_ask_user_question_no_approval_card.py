"""ask_user_question 不能走审批卡片 —— 它自己就是提问 UI。

## 真机实测发现的缺陷（2026-11-14）

用 JSONL bridge 驱动真实 GA（deepseek-latest-flash），模型正确调用了
`ask_user_question`，参数也对，但事件流里出现的是：

    permission_request | toolName: "ask_user_question" ... (always_ask)
    [Permission] waiting for approval: ask_user_question (always_ask)

也就是说权限层把它当成普通工具拦下来，弹了一张**审批卡片**（accept/deny），
用户必须先点「允许」才能看到真正的问题。如果点「拒绝」，问题就永远不会出现。

这是「双重询问」：先问「你允许我问你问题吗」，再问真正的问题。

## 根因

方案 §4.5 原本写「恒 ask」以防 full_access 吞掉它。但「ask」在 GA 里
的语义是「弹审批卡片」，不是「弹提问卡片」。CC 能这么做是因为它的
`requiresUserInteraction()` 让权限层把 ask 理解成「用工具自己的 UI」；
GA 的权限层没有这个概念，ask 就是通用的 accept/deny 卡片。

## 正确做法

**放行**（allow），让工具自己负责交互。理由：
- 提问不改变任何状态，不需要授权；
- 用户的「回答」本身就是对这次交互的许可；
- 只读档下也应该能问（问问题不是写操作）。

本文件锁住这个行为：dispatch 到 ask_user_question 时**不得**产生
permission_request，必须直接进到工具实现。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from genericagent.core.handler import GenericAgentHandler
from genericagent.core.loop import StepOutcome, exhaust
from genericagent.core.permissions.policy import (
    ASK,
    FULL_ACCESS,
    READ_ONLY,
    build_permission_mode_policy,
)


def _handler(mode: str = FULL_ACCESS) -> tuple[GenericAgentHandler, list[dict]]:
    """构造 handler，并记录所有 emit 出去的事件。"""
    parent = SimpleNamespace(
        task_dir=".", stop_sig=False, llmclient=None,
        session_id=None, session_path=None, permission_mode=mode,
        session_turn_id=0, agent_id="t", agent_type="t", is_subagent=False,
    )
    h = GenericAgentHandler(parent, cwd=".", workspace_root=None)
    h.permission_mode_policy = build_permission_mode_policy(mode)

    emitted: list[dict] = []
    from genericagent.core.ask_user_question.runtime import QuestionRuntime

    rt = QuestionRuntime()
    rt.set_emit(emitted.append)
    h.question_runtime = rt

    # 让提问立即返回，避免测试真的阻塞
    from genericagent.core.ask_user_question.runtime import QuestionResult

    rt.ask = lambda qs, **kw: QuestionResult(status="cancelled", message="test")  # type: ignore

    return h, emitted


ARGS = {"questions": [{"key": "task", "question": "Which first?", "options": [{"label": "a"}]}]}


class NoApprovalCardTest(unittest.TestCase):
    def test_no_permission_request_in_full_access(self):
        """full_access 下不该弹审批卡片。"""
        h, emitted = _handler(FULL_ACCESS)
        exhaust(h.dispatch("ask_user_question", dict(ARGS), None, 0, 1))
        kinds = [e.get("type") for e in emitted]
        self.assertNotIn(
            "permission_request",
            kinds,
            f"dispatch 弹了审批卡片，用户会先被问「允许我问你吗」：{kinds}",
        )

    def test_no_permission_request_in_ask_mode(self):
        h, emitted = _handler(ASK)
        exhaust(h.dispatch("ask_user_question", dict(ARGS), None, 0, 1))
        kinds = [e.get("type") for e in emitted]
        self.assertNotIn("permission_request", kinds, f"ask 档弹了审批卡片：{kinds}")

    def test_no_permission_request_in_read_only(self):
        """只读档也要能提问 —— 问问题不是写操作。"""
        h, emitted = _handler(READ_ONLY)
        exhaust(h.dispatch("ask_user_question", dict(ARGS), None, 0, 1))
        kinds = [e.get("type") for e in emitted]
        self.assertNotIn("permission_request", kinds, f"read_only 档弹了审批卡片：{kinds}")

    def test_dispatch_actually_reaches_the_tool(self):
        """关键：dispatch 必须真的走到 do_ask_user_question。
        否则「没弹审批卡片」可能只是因为压根没执行。"""
        h, _ = _handler(ASK)

        called = {"n": 0}
        original = h.do_ask_user_question

        def spy(args, response):
            called["n"] += 1
            return original(args, response)

        h.do_ask_user_question = spy  # type: ignore
        exhaust(h.dispatch("ask_user_question", dict(ARGS), None, 0, 1))
        self.assertEqual(1, called["n"], "dispatch 没有调到工具实现")

    def test_read_only_does_not_deny_the_tool(self):
        """只读档不能把提问拒掉（否则等于禁用了提问）。"""
        h, _ = _handler(READ_ONLY)
        outcome = exhaust(h.dispatch("ask_user_question", dict(ARGS), None, 0, 1))
        blob = str(outcome.data) + str(outcome.next_prompt)
        self.assertNotIn("read_only_blocked", blob)


class PolicyLevelTest(unittest.TestCase):
    """策略层：所有档位都 allow。"""

    def test_policy_allows_in_every_mode(self):
        for mode in (READ_ONLY, ASK, FULL_ACCESS):
            decision = build_permission_mode_policy(mode).evaluate("ask_user_question", {})
            self.assertEqual(
                "allow",
                decision.action,
                f"{mode} 档下 ask_user_question 被决策为 {decision.action}，应当 allow",
            )

    def test_other_tools_still_follow_the_mode(self):
        """别顺手把其他工具也放行。"""
        self.assertEqual(
            "deny", build_permission_mode_policy(READ_ONLY).evaluate("file_write", {}).action
        )
        self.assertEqual(
            "ask", build_permission_mode_policy(ASK).evaluate("file_write", {}).action
        )


if __name__ == "__main__":
    unittest.main()
