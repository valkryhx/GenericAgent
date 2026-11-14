"""do_ask_user_question：在工具调用内部阻塞拿答案，不结束回合。

与 do_ask_user 的关键差异（实施方案 §0 / §4.4）：
- 不设 should_exit —— 答案作为本次 tool_result 回来，模型同一轮继续；
- 三种结局（submitted / cancelled / unavailable）各有不同话术；
- 答案过准入，失败时把拒绝原因告诉模型而不是静默丢弃。
"""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from genericagent.core.ask_user_question.runtime import (
    CANCELLED,
    SUBMITTED,
    UNAVAILABLE,
    QuestionResult,
    QuestionRuntime,
)
from genericagent.core.handler import GenericAgentHandler
from genericagent.core.loop import StepOutcome, exhaust


def make_handler(runtime: QuestionRuntime | None = None) -> GenericAgentHandler:
    """构造一个只够跑 do_ask_user_question 的 handler。"""
    parent = SimpleNamespace(
        task_dir=".", stop_sig=False, llmclient=None,
        session_id=None, session_path=None, permission_mode=None,
        session_turn_id=0, agent_id="test", agent_type="test", is_subagent=False,
    )
    h = GenericAgentHandler(parent, cwd=".", workspace_root=None)
    h.question_runtime = runtime
    return h


QUESTIONS = [
    {"key": "q1", "question": "Which DB?", "options": ["postgres", "sqlite"]},
]


class DoAskUserQuestionTest(unittest.TestCase):
    def test_headless_returns_unavailable_not_fatal(self):
        """没有 UI 时不崩、不退出回合，返回一个模型能读懂的说明。"""
        h = make_handler(QuestionRuntime())
        outcome = exhaust(h.do_ask_user_question({"questions": QUESTIONS}, None))
        self.assertIsInstance(outcome, StepOutcome)
        self.assertFalse(outcome.should_exit)
        self.assertEqual("unavailable", outcome.data["status"])
        self.assertIn("not a refusal", str(outcome.next_prompt).lower())

    def test_submitted_returns_answers(self):
        rt = QuestionRuntime()
        rt.set_emit(lambda ev: None)
        h = make_handler(rt)

        # 打桩：直接让 runtime.ask 返回已提交
        rt.ask = lambda qs, **kw: QuestionResult(
            status=SUBMITTED, answers={"q1": "postgres"}
        )
        outcome = exhaust(h.do_ask_user_question({"questions": QUESTIONS}, None))
        self.assertFalse(outcome.should_exit)
        self.assertIn("postgres", str(outcome.data))

    def test_never_sets_should_exit(self):
        """核心差异：不能像 ask_user 那样结束回合。"""
        rt = QuestionRuntime()
        rt.set_emit(lambda ev: None)
        h = make_handler(rt)
        rt.ask = lambda qs, **kw: QuestionResult(status=CANCELLED)
        outcome = exhaust(h.do_ask_user_question({"questions": QUESTIONS}, None))
        self.assertFalse(outcome.should_exit)

    def test_missing_questions_is_an_error(self):
        h = make_handler(QuestionRuntime())
        outcome = exhaust(h.do_ask_user_question({}, None))
        self.assertFalse(outcome.should_exit)
        self.assertIn("error", str(outcome.data).lower())

    def test_questions_without_key_is_an_error(self):
        h = make_handler(QuestionRuntime())
        outcome = exhaust(
            h.do_ask_user_question({"questions": [{"question": "no key"}]}, None)
        )
        self.assertIn("error", str(outcome.data).lower())

    def test_too_many_questions_rejected(self):
        h = make_handler(QuestionRuntime())
        many = [{"key": f"q{i}", "question": f"Q{i}?"} for i in range(6)]
        outcome = exhaust(h.do_ask_user_question({"questions": many}, None))
        self.assertIn("error", str(outcome.data).lower())

    def test_no_runtime_is_unavailable(self):
        h = make_handler(None)
        outcome = exhaust(h.do_ask_user_question({"questions": QUESTIONS}, None))
        self.assertFalse(outcome.should_exit)
        self.assertEqual("unavailable", outcome.data["status"])


class SingleQuestionShortcutTest(unittest.TestCase):
    """hide_submit_tab 的判定在 Python 侧，用于前端协议。"""

    def test_single_non_multiselect_hides_submit_tab(self):
        from genericagent.core.ask_user_question import hide_submit_tab

        self.assertTrue(
            hide_submit_tab([{"key": "q1", "multiSelect": False}])
        )

    def test_single_multiselect_keeps_submit_tab(self):
        from genericagent.core.ask_user_question import hide_submit_tab

        self.assertFalse(hide_submit_tab([{"key": "q1", "multiSelect": True}]))

    def test_multiple_questions_keep_submit_tab(self):
        from genericagent.core.ask_user_question import hide_submit_tab

        self.assertFalse(
            hide_submit_tab([{"key": "q1"}, {"key": "q2"}])
        )


if __name__ == "__main__":
    unittest.main()
