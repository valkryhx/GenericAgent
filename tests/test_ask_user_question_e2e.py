"""端到端：模型调用 → 卡片 → 用户作答 → 模型在同一轮拿到答案。

这是整个特性的验收测试。它不 mock 任何一层：
handler 真的调 runtime，runtime 真的发 question_request，
"用户" 那条线程真的调用 bridge.question_response，答案真的过准入，
最后 tool_result 真的带着答案回到 handler。

只 mock 一件事：没有真的 LLM —— `do_ask_user_question` 是被直接调用的，
这正是模型调用它时走的那条路径。
"""

from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from genericagent.core.ask_user_question.runtime import QuestionRuntime  # noqa: E402
from genericagent.core.handler import GenericAgentHandler  # noqa: E402
from genericagent.core.loop import StepOutcome, exhaust  # noqa: E402


def _handler_with_runtime() -> tuple[GenericAgentHandler, QuestionRuntime]:
    parent = SimpleNamespace(
        task_dir=".", stop_sig=False, llmclient=None,
        session_id=None, session_path=None, permission_mode=None,
        session_turn_id=0, agent_id="e2e", agent_type="e2e", is_subagent=False,
    )
    handler = GenericAgentHandler(parent, cwd=".", workspace_root=None)
    runtime = QuestionRuntime()
    # 接上一个「UI」：没有 emit 时 ask() 会正确地返回 unavailable（headless）。
    # 端到端测试必须模拟有界面的情况，所以这里注入一个 no-op emit；
    # 用户那一侧通过 runtime.pending_ids() 轮询，不需要从 emit 里拿 id。
    runtime.set_emit(lambda event: None)
    handler.question_runtime = runtime
    return handler, runtime


class EndToEndTest(unittest.TestCase):
    def test_single_question_roundtrip(self):
        """单题：答完立即提交，tool_result 带答案回来，回合不结束。"""
        handler, runtime = _handler_with_runtime()

        seen: dict = {}

        def fake_user():
            # 等卡片出现
            for _ in range(300):
                ids = runtime.pending_ids()
                if ids:
                    seen["requestId"] = ids[0]
                    break
                time.sleep(0.01)
            else:
                return
            # 模拟用户在 UI 上点选
            runtime.submit(seen["requestId"], {"answers": {"db": "postgres"}})

        user = threading.Thread(target=fake_user)
        user.start()

        outcome = exhaust(
            handler.do_ask_user_question(
                {
                    "questions": [
                        {"key": "db", "question": "Which DB?", "options": [{"label": "postgres"}]}
                    ]
                },
                None,
            )
        )
        user.join(timeout=5.0)

        self.assertIsInstance(outcome, StepOutcome)
        self.assertFalse(outcome.should_exit, "不该结束回合")
        self.assertEqual("answered", outcome.data["status"])
        self.assertEqual({"db": "postgres"}, outcome.data["answers"])
        # 模型看到的是可读句子，不是裸 JSON
        self.assertIn("Which DB?", outcome.next_prompt)
        self.assertIn("postgres", outcome.next_prompt)

    def test_multi_question_all_answered(self):
        handler, runtime = _handler_with_runtime()

        def fake_user():
            for _ in range(300):
                ids = runtime.pending_ids()
                if ids:
                    break
                time.sleep(0.01)
            else:
                return
            runtime.submit(ids[0], {"answers": {"db": "sqlite", "region": "eu"}})

        user = threading.Thread(target=fake_user)
        user.start()

        outcome = exhaust(
            handler.do_ask_user_question(
                {
                    "questions": [
                        {"key": "db", "question": "Which DB?"},
                        {"key": "region", "question": "Which region?"},
                    ]
                },
                None,
            )
        )
        user.join(timeout=5.0)

        self.assertEqual("answered", outcome.data["status"])
        self.assertEqual({"db": "sqlite", "region": "eu"}, outcome.data["answers"])
        self.assertIn("All questions were answered", outcome.next_prompt)

    def test_partial_answer_is_reported_as_undecided(self):
        """用户可以只答一部分；未答的必须被说成「未定」，不是「选了默认」。"""
        handler, runtime = _handler_with_runtime()

        def fake_user():
            for _ in range(300):
                ids = runtime.pending_ids()
                if ids:
                    break
                time.sleep(0.01)
            else:
                return
            runtime.submit(ids[0], {"answers": {"db": "sqlite"}})

        user = threading.Thread(target=fake_user)
        user.start()

        outcome = exhaust(
            handler.do_ask_user_question(
                {
                    "questions": [
                        {"key": "db", "question": "Which DB?"},
                        {"key": "region", "question": "Which region?"},
                    ]
                },
                None,
            )
        )
        user.join(timeout=5.0)

        self.assertEqual("answered", outcome.data["status"])
        self.assertIn("Left unanswered: region", outcome.next_prompt)
        self.assertIn("not as a default choice", outcome.next_prompt)

    def test_tampered_answer_never_reaches_the_model(self):
        """有人往 answers 里塞没问过的题 → 准入拒绝，模型拿不到它。"""
        handler, runtime = _handler_with_runtime()

        state: dict = {}

        def fake_user():
            for _ in range(300):
                ids = runtime.pending_ids()
                if ids:
                    break
                time.sleep(0.01)
            else:
                return
            # 篡改：塞一个没问过的 key
            state["tampered_ok"] = runtime.submit(
                ids[0], {"answers": {"db": "postgres", "q_injected": "evil"}}
            )
            # 再正常作答
            runtime.submit(ids[0], {"answers": {"db": "postgres"}})

        user = threading.Thread(target=fake_user)
        user.start()

        outcome = exhaust(
            handler.do_ask_user_question(
                {"questions": [{"key": "db", "question": "Which DB?"}]}, None
            )
        )
        user.join(timeout=5.0)

        self.assertFalse(state["tampered_ok"], "篡改的 payload 不该被接受")
        self.assertNotIn("q_injected", str(outcome.data))
        self.assertNotIn("evil", str(outcome.data))

    def test_stop_mid_question_cancels_cleanly(self):
        """用户按 /stop：卡片收起，工具返回 cancelled，不是崩。"""
        handler, runtime = _handler_with_runtime()
        handler.parent.stop_sig = False

        def stopper():
            for _ in range(300):
                if runtime.pending_ids():
                    break
                time.sleep(0.01)
            else:
                return
            handler.parent.stop_sig = True

        t = threading.Thread(target=stopper)
        t.start()

        outcome = exhaust(
            handler.do_ask_user_question(
                {"questions": [{"key": "db", "question": "Which DB?"}]}, None
            )
        )
        t.join(timeout=5.0)

        self.assertFalse(outcome.should_exit)
        self.assertEqual("cancelled", outcome.data["status"])


if __name__ == "__main__":
    unittest.main()
