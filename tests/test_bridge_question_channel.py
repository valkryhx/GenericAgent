"""bridge 层接线：question_request / question_response。

对标 permission 通道（实施方案 §7.1）：

| permission | question |
| --- | --- |
| `permission_runtime.set_emit` | `question_runtime.set_emit` |
| `permission_response(requestId, decision)` | `question_response(requestId, answers)` |

why 新通道而不是复用 permission_request：
载荷形状完全不同（一个是 accept/deny，一个是「多题 × 多选 × 可输入」），
硬塞会让 approvalPanel 的语义分叉（实施方案 §7.2）。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


def _bridge():
    from frontends.ink.bridge import GenericAgentBridge

    agent = mock.MagicMock()
    agent.is_running = False
    bridge = GenericAgentBridge.__new__(GenericAgentBridge)
    bridge.agent = agent
    bridge.emit = mock.MagicMock()
    return bridge, agent


class QuestionResponseTest(unittest.TestCase):
    def test_question_response_submits_answers(self):
        from genericagent.core.ask_user_question.runtime import QuestionRuntime

        bridge, agent = _bridge()
        rt = QuestionRuntime()
        rt.set_emit(lambda ev: None)
        agent.question_runtime = rt

        # 造一个挂起的请求
        import threading

        questions = [{"key": "q1", "question": "Which DB?"}]
        box: dict = {}

        def worker():
            box["result"] = rt.ask(questions, timeout_s=5.0)

        t = threading.Thread(target=worker)
        t.start()
        import time

        for _ in range(100):
            if rt.pending_ids():
                break
            time.sleep(0.01)
        rid = rt.pending_ids()[0]

        bridge.question_response(rid, {"answers": {"q1": "postgres"}})
        t.join(timeout=5.0)

        self.assertEqual("submitted", box["result"].status)
        self.assertEqual({"q1": "postgres"}, box["result"].answers)

    def test_unknown_request_id_emits_error(self):
        from genericagent.core.ask_user_question.runtime import QuestionRuntime

        bridge, agent = _bridge()
        rt = QuestionRuntime()
        rt.set_emit(lambda ev: None)
        agent.question_runtime = rt
        bridge.question_response("nope", {"answers": {}})
        args = bridge.emit.call_args[0][0]
        self.assertEqual("error", args["type"])
        self.assertEqual("question_response_rejected", args["code"])

    def test_no_runtime_emits_error(self):
        bridge, agent = _bridge()
        agent.question_runtime = None
        bridge.question_response("x", {"answers": {}})
        args = bridge.emit.call_args[0][0]
        self.assertEqual("error", args["type"])

    def test_tampered_payload_emits_error_and_keeps_pending(self):
        from genericagent.core.ask_user_question.runtime import QuestionRuntime

        bridge, agent = _bridge()
        rt = QuestionRuntime()
        rt.set_emit(lambda ev: None)
        agent.question_runtime = rt

        import threading
        import time

        questions = [{"key": "q1", "question": "Which DB?"}]

        def worker():
            rt.ask(questions, timeout_s=0.5)

        t = threading.Thread(target=worker)
        t.start()
        for _ in range(100):
            if rt.pending_ids():
                break
            time.sleep(0.01)
        rid = rt.pending_ids()[0]

        bridge.question_response(rid, {"answers": {"q_hacked": "x"}})
        args = bridge.emit.call_args[0][0]
        self.assertEqual("error", args["type"])
        self.assertEqual(1, rt.pending_count())
        t.join(timeout=5.0)


class CommandDispatchTest(unittest.TestCase):
    def test_question_response_command_routed(self):
        """确认 command 分发里认得 question_response。"""
        src = (REPO_ROOT / "frontends" / "ink" / "bridge.py").read_text(encoding="utf-8")
        self.assertIn('"question_response"', src)
        self.assertIn("def question_response", src)


if __name__ == "__main__":
    unittest.main()
