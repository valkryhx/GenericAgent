"""question_response 的取消语义与聊天出口。

两种「非作答」要分开处理：
- `cancelled: true` → 用户按 Esc，走普通 deny（调研 §9.4）
- 「Chat about this」→ 也是取消，但要给模型一段**说明为什么会话中断**的话术

CC 的做法是 `deny + feedback`（调研 §9.3），不是伪造一个答案。
GA 这里同理：`cancelled` 走 runtime.cancel()，由 handler 生成话术。
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from genericagent.core.ask_user_question.runtime import (  # noqa: E402
    CANCELLED,
    SUBMITTED,
    QuestionRuntime,
)


def _bridge():
    from frontends.ink.bridge import GenericAgentBridge

    agent = mock.MagicMock()
    agent.is_running = False
    bridge = GenericAgentBridge.__new__(GenericAgentBridge)
    bridge.agent = agent
    bridge.emit = mock.MagicMock()
    return bridge, agent


def _start_pending(rt: QuestionRuntime, questions: list[dict]):
    box: dict = {}

    def worker():
        box["result"] = rt.ask(questions, timeout_s=5.0)

    t = threading.Thread(target=worker)
    t.start()
    for _ in range(200):
        if rt.pending_ids():
            break
        time.sleep(0.01)
    return t, box


class CancelledResponseTest(unittest.TestCase):
    def test_cancelled_true_cancels_instead_of_submitting(self):
        bridge, agent = _bridge()
        rt = QuestionRuntime()
        rt.set_emit(lambda ev: None)
        agent.question_runtime = rt

        t, box = _start_pending(rt, [{"key": "q1", "question": "?"}])
        rid = rt.pending_ids()[0]

        bridge.question_response(rid, {}, cancelled=True)
        t.join(timeout=5.0)

        self.assertEqual(CANCELLED, box["result"].status)

    def test_cancelled_with_empty_answers_does_not_touch_admit(self):
        """取消不该被准入拒绝成「非法 payload」——它根本不是一次作答。"""
        bridge, agent = _bridge()
        rt = QuestionRuntime()
        rt.set_emit(lambda ev: None)
        agent.question_runtime = rt

        t, box = _start_pending(rt, [{"key": "q1", "question": "?"}])
        rid = rt.pending_ids()[0]

        bridge.question_response(rid, {}, cancelled=True)
        t.join(timeout=5.0)
        # 不该发 error
        for call in bridge.emit.call_args_list:
            self.assertNotEqual("error", call[0][0].get("type"))


class ChatAboutThisTest(unittest.TestCase):
    """Chat 出口 = 取消 + 一段让模型 Asking 用户想澄清什么的话术。"""

    def test_chat_exit_is_a_cancel_with_feedback(self):
        from genericagent.core.ask_user_question.admit import CHAT_ABOUT_THIS_PROMPT

        self.assertIn("clarify", CHAT_ABOUT_THIS_PROMPT.lower())
        self.assertIn("ask", CHAT_ABOUT_THIS_PROMPT.lower())

    def test_cancel_carries_a_reason(self):
        """取消要能区分「Esc 退出」与「我想聊聊」，两者给模型的话不同。"""
        rt = QuestionRuntime()
        rt.set_emit(lambda ev: None)
        t, box = _start_pending(rt, [{"key": "q1", "question": "?"}])
        rid = rt.pending_ids()[0]
        rt.cancel(rid, reason="chat")
        t.join(timeout=5.0)
        self.assertEqual(CANCELLED, box["result"].status)
        self.assertEqual("chat", box["result"].reason)

    def test_plain_cancel_has_no_reason(self):
        rt = QuestionRuntime()
        rt.set_emit(lambda ev: None)
        t, box = _start_pending(rt, [{"key": "q1", "question": "?"}])
        rid = rt.pending_ids()[0]
        rt.cancel(rid)
        t.join(timeout=5.0)
        self.assertEqual("", box["result"].reason)

    def test_handler_turns_chat_reason_into_feedback_prompt(self):
        """handler 看到 reason=chat 时，回给模型的是 CHAT_ABOUT_THIS_PROMPT。"""
        from genericagent.core.ask_user_question.admit import CHAT_ABOUT_THIS_PROMPT
        from genericagent.core.ask_user_question.runtime import QuestionResult
        from genericagent.core.handler import GenericAgentHandler
        from genericagent.core.loop import exhaust
        from types import SimpleNamespace

        parent = SimpleNamespace(
            task_dir=".", stop_sig=False, llmclient=None,
            session_id=None, session_path=None, permission_mode=None,
            session_turn_id=0, agent_id="t", agent_type="t", is_subagent=False,
        )
        h = GenericAgentHandler(parent, cwd=".", workspace_root=None)
        rt = QuestionRuntime()
        rt.set_emit(lambda ev: None)
        h.question_runtime = rt
        rt.ask = lambda qs, **kw: QuestionResult(
            status=CANCELLED, reason="chat", message=""
        )

        outcome = exhaust(h.do_ask_user_question({"questions": [{"key": "q1", "question": "?"}]}, None))
        self.assertIn(CHAT_ABOUT_THIS_PROMPT, outcome.next_prompt)


if __name__ == "__main__":
    unittest.main()
