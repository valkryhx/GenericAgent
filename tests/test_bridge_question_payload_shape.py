"""bridge 命令的 `answers` 字段 vs admit 的 payload 形状。

## 真机实测发现的缺陷（2026-11-14）

UI 发的命令是：

    {"type": "question_response", "requestId": "...", "answers": {"task": "write tests"}}

bridge 却把这层 `answers` 直接当成 payload 传给 `runtime.submit()`，
而 `admit()` 期望的 payload 是 `{"answers": {"task": "write tests"}}`。
于是 `task` 被当成「未知顶层键」，整个回答被拒：

    {"type": "error", "code": "question_response_rejected",
     "message": "unknown requestId or rejected answer: 6bd0..."}

用户答了，卡片收起了，但答案没到 LLM —— 正是「白做了」的那种失败。

单元测试没抓到这个，因为它们是直接调 `runtime.submit(rid, {"answers": ...})`，
绕过了 bridge 这一层的字段错位。
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
    SUBMITTED,
    QuestionRuntime,
)

QUESTIONS = [{"key": "task", "question": "Which first?", "options": [{"label": "write tests"}]}]


def _bridge(runtime: QuestionRuntime):
    from frontends.ink.bridge import GenericAgentBridge

    agent = mock.MagicMock()
    agent.is_running = False
    agent.question_runtime = runtime
    bridge = GenericAgentBridge.__new__(GenericAgentBridge)
    bridge.agent = agent
    bridge.emit = mock.MagicMock()
    return bridge


def _pending(runtime: QuestionRuntime):
    box: dict = {}

    def worker():
        box["result"] = runtime.ask(QUESTIONS, timeout_s=5.0)

    t = threading.Thread(target=worker)
    t.start()
    for _ in range(300):
        if runtime.pending_ids():
            break
        time.sleep(0.01)
    return t, box


class AnswersFieldShapeTest(unittest.TestCase):
    def test_bridge_accepts_the_ui_command_shape(self):
        """UI 发的是 `answers` 字段；bridge 必须把它包成 admit 认识的 payload。"""
        rt = QuestionRuntime()
        rt.set_emit(lambda ev: None)
        bridge = _bridge(rt)
        t, box = _pending(rt)
        rid = rt.pending_ids()[0]

        # 完全模拟 UI 发来的命令：answers 是独立的顶层字段
        bridge.question_response(rid, {"task": "write tests"})
        t.join(timeout=5.0)

        self.assertEqual(
            SUBMITTED,
            box["result"].status,
            f"UI 形状的回答被拒了：{box['result']}",
        )
        self.assertEqual({"task": "write tests"}, box["result"].answers)

    def test_no_error_event_on_a_valid_answer(self):
        rt = QuestionRuntime()
        rt.set_emit(lambda ev: None)
        bridge = _bridge(rt)
        t, _ = _pending(rt)
        rid = rt.pending_ids()[0]
        bridge.question_response(rid, {"task": "write tests"})
        t.join(timeout=5.0)

        for call in bridge.emit.call_args_list:
            ev = call[0][0]
            self.assertNotEqual(
                "error", ev.get("type"), f"合法回答不该产生 error：{ev}"
            )

    def test_explicit_wrapped_payload_still_works(self):
        """已经包成 `{"answers": ...}` 的调用方不能被这个修复弄坏。"""
        rt = QuestionRuntime()
        rt.set_emit(lambda ev: None)
        bridge = _bridge(rt)
        t, box = _pending(rt)
        rid = rt.pending_ids()[0]
        bridge.question_response(rid, {"answers": {"task": "write docs"}})
        t.join(timeout=5.0)
        self.assertEqual(SUBMITTED, box["result"].status)
        self.assertEqual({"task": "write docs"}, box["result"].answers)

    def test_unknown_key_still_rejected(self):
        """修复不能放松准入：没问过的 key 仍必须被拒。"""
        rt = QuestionRuntime()
        rt.set_emit(lambda ev: None)
        bridge = _bridge(rt)
        t, _ = _pending(rt)
        rid = rt.pending_ids()[0]
        bridge.question_response(rid, {"hacked": "evil"})
        self.assertEqual(1, rt.pending_count(), "篡改的答案不该让请求结算")
        rt.cancel(rid)
        t.join(timeout=5.0)


if __name__ == "__main__":
    unittest.main()
