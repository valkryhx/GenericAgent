"""QuestionRuntime：阻塞等用户作答，对标 PermissionRuntime 但载荷不同。

与 PermissionRuntime 的关键差异（实施方案 §4.1）：
- 载荷是「多题 × 多选 × 可输入」，不是「一个工具名 + accept/deny」；
- 用户答案要过准入（`admit`），不是一个布尔值；
- 无 emit（headless）时**不是 deny**，而是返回一个明确的 `unavailable`
  ——模型需要知道「问不了」，而不是「用户拒绝了」。

后者尤其重要：把 headless 当成「用户说不」会让模型误以为用户考虑过这个问题。
"""

from __future__ import annotations

import threading
import unittest
from concurrent.futures import Future

from genericagent.core.ask_user_question.runtime import (
    CANCELLED,
    SUBMITTED,
    UNAVAILABLE,
    QuestionRuntime,
)


QUESTIONS = [
    {"key": "q1", "question": "Which DB?", "options": ["postgres", "sqlite"]},
]


class HeadlessTest(unittest.TestCase):
    def test_no_emit_returns_unavailable_not_deny(self):
        """headless 不是「用户拒绝」，是「问不了」。"""
        rt = QuestionRuntime()
        result = rt.ask(QUESTIONS, timeout_s=1.0)
        self.assertEqual(UNAVAILABLE, result.status)
        self.assertEqual({}, result.answers)
        self.assertTrue(result.message)

    def test_has_emit_reports_capability(self):
        rt = QuestionRuntime()
        self.assertFalse(rt.has_emit())
        rt.set_emit(lambda ev: None)
        self.assertTrue(rt.has_emit())


class SubmitTest(unittest.TestCase):
    def test_submit_unblocks_ask(self):
        events: list[dict] = []
        rt = QuestionRuntime()
        rt.set_emit(events.append)

        box: dict = {}

        def worker():
            box["result"] = rt.ask(QUESTIONS, timeout_s=5.0)

        t = threading.Thread(target=worker)
        t.start()

        request_id = _wait_for_request(rt)
        self.assertTrue(rt.submit(request_id, {"answers": {"q1": "postgres"}}))
        t.join(timeout=5.0)

        self.assertEqual(SUBMITTED, box["result"].status)
        self.assertEqual({"q1": "postgres"}, box["result"].answers)
        self.assertEqual("question_request", events[0]["type"])
        self.assertEqual("q1", events[0]["questions"][0]["key"])

    def test_submit_with_tampered_payload_is_rejected(self):
        """准入在 runtime 内部执行 —— 前端不可绕过。"""
        rt = QuestionRuntime()
        rt.set_emit(lambda ev: None)
        box: dict = {}

        def worker():
            box["result"] = rt.ask(QUESTIONS, timeout_s=5.0)

        t = threading.Thread(target=worker)
        t.start()
        request_id = _wait_for_request(rt)

        ok = rt.submit(request_id, {"answers": {"q_not_asked": "x"}})
        self.assertFalse(ok)
        # 拒绝后请求仍然挂起，等用户重发
        self.assertEqual(1, rt.pending_count())
        rt.cancel(request_id)
        t.join(timeout=5.0)
        self.assertEqual(CANCELLED, box["result"].status)

    def test_cancel_marks_cancelled(self):
        rt = QuestionRuntime()
        rt.set_emit(lambda ev: None)
        box: dict = {}

        def worker():
            box["result"] = rt.ask(QUESTIONS, timeout_s=5.0)

        t = threading.Thread(target=worker)
        t.start()
        request_id = _wait_for_request(rt)
        self.assertTrue(rt.cancel(request_id))
        t.join(timeout=5.0)
        self.assertEqual(CANCELLED, box["result"].status)

    def test_cancel_all_on_stop(self):
        rt = QuestionRuntime()
        rt.set_emit(lambda ev: None)
        box: dict = {}

        def worker():
            box["result"] = rt.ask(QUESTIONS, timeout_s=5.0)

        t = threading.Thread(target=worker)
        t.start()
        _wait_for_request(rt)
        self.assertEqual(1, rt.cancel_all())
        t.join(timeout=5.0)
        self.assertEqual(CANCELLED, box["result"].status)

    def test_stop_check_aborts(self):
        rt = QuestionRuntime()
        rt.set_emit(lambda ev: None)
        stop = {"v": False}
        box: dict = {}

        def worker():
            box["result"] = rt.ask(QUESTIONS, timeout_s=5.0, stop_check=lambda: stop["v"])

        t = threading.Thread(target=worker)
        t.start()
        _wait_for_request(rt)
        stop["v"] = True
        t.join(timeout=5.0)
        self.assertEqual(CANCELLED, box["result"].status)

    def test_timeout_returns_cancelled_and_cleans_up(self):
        rt = QuestionRuntime()
        rt.set_emit(lambda ev: None)
        result = rt.ask(QUESTIONS, timeout_s=0.2)
        self.assertEqual(CANCELLED, result.status)
        self.assertEqual(0, rt.pending_count())

    def test_timeout_actually_unblocks_the_caller(self):
        """拆掉超时后这条会挂起 —— 用后台线程 + join 上限把它变成失败而不是卡死。"""
        rt = QuestionRuntime()
        rt.set_emit(lambda ev: None)
        done: dict = {}

        def worker():
            done["result"] = rt.ask(QUESTIONS, timeout_s=0.3)

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        t.join(timeout=3.0)
        self.assertFalse(t.is_alive(), "ask() did not return within 3s despite timeout_s=0.3")
        self.assertEqual(CANCELLED, done["result"].status)

    def test_double_submit_only_counts_once(self):
        rt = QuestionRuntime()
        rt.set_emit(lambda ev: None)
        box: dict = {}

        def worker():
            box["result"] = rt.ask(QUESTIONS, timeout_s=5.0)

        t = threading.Thread(target=worker)
        t.start()
        request_id = _wait_for_request(rt)
        self.assertTrue(rt.submit(request_id, {"answers": {"q1": "postgres"}}))
        self.assertFalse(rt.submit(request_id, {"answers": {"q1": "sqlite"}}))
        t.join(timeout=5.0)
        self.assertEqual("postgres", box["result"].answers["q1"])


def _wait_for_request(rt: QuestionRuntime, timeout: float = 3.0) -> str:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        pending = rt.pending_ids()
        if pending:
            return pending[0]
        time.sleep(0.01)
    raise AssertionError("no pending question request appeared")


if __name__ == "__main__":
    unittest.main()
