"""ask_user_question 准入层：只有「回答」能通过，改题/改选项/改预览一律拒绝。

对标 CC 的 AskUserQuestion `admitCardAnswer`（调研文档 §3）。
GA 无条件执行（CC 只在 strict 分支上执行，见调研 §3.1.0），所以这里更严格。

设计约束（来自实施方案 §5.2 四条规则）：
1. 只允许 answers / annotations / response / followUp 四个顶层键
2. 每个答案必须对应一个已问的题；不许新增题、不许改题面
3. 答案长度有上限，总量有预算
4. 笔记只能写 notes，不许借笔记改写答案

任何一条不满足 → 拒绝并给出可回给模型的话术。
"""

from __future__ import annotations

import unittest

from genericagent.core.ask_user_question.admit import (
    MAX_ANSWERS_TOTAL,
    MAX_STRING,
    NOTES_ONLY,
    admit,
    finalize,
    refusal_prompt,
)


QUESTIONS = [
    {"key": "q1", "question": "Which DB?", "options": ["postgres", "sqlite"]},
    {"key": "q2", "question": "Which region?", "options": ["us", "eu"]},
]


class AdmitAcceptTest(unittest.TestCase):
    def test_plain_answers_accepted(self):
        ok, payload, reason = admit(QUESTIONS, {"answers": {"q1": "postgres"}})
        self.assertTrue(ok)
        self.assertEqual({"q1": "postgres"}, payload["answers"])
        self.assertEqual("", reason)

    def test_multiselect_answer_as_list(self):
        ok, payload, _ = admit(QUESTIONS, {"answers": {"q1": ["postgres", "sqlite"]}})
        self.assertTrue(ok)
        self.assertEqual(["postgres", "sqlite"], payload["answers"]["q1"])

    def test_unanswered_question_is_fine(self):
        """未答完允许提交 —— 调研 §9.7.5。"""
        ok, payload, _ = admit(QUESTIONS, {"answers": {}})
        self.assertTrue(ok)
        self.assertEqual({}, payload["answers"])

    def test_user_typed_text_is_a_valid_answer(self):
        """用户可以不选选项、直接写字。"""
        ok, payload, _ = admit(QUESTIONS, {"answers": {"q1": "we use oracle actually"}})
        self.assertTrue(ok)
        self.assertEqual("we use oracle actually", payload["answers"]["q1"])

    def test_notes_only_sentinel(self):
        """只写备注不选答案 —— 方案 §5.2。"""
        ok, payload, _ = admit(
            QUESTIONS, {"answers": {"q1": NOTES_ONLY}, "annotations": {"q1": {"notes": "想想"}}}
        )
        self.assertTrue(ok)
        self.assertEqual(NOTES_ONLY, payload["answers"]["q1"])


class AdmitRejectTest(unittest.TestCase):
    def test_unknown_top_level_key_rejected(self):
        ok, _, reason = admit(QUESTIONS, {"answers": {"q1": "postgres"}, "hacked": 1})
        self.assertFalse(ok)
        self.assertIn("hacked", reason)

    def test_unknown_question_key_rejected(self):
        """不许回答问题之外的题 —— 防模型/宿主注入新题。"""
        ok, _, reason = admit(QUESTIONS, {"answers": {"q99": "postgres"}})
        self.assertFalse(ok)
        self.assertIn("q99", reason)

    def test_cannot_rewrite_question_text(self):
        ok, _, reason = admit(QUESTIONS, {"answers": {}, "questions": [{"key": "q1"}]})
        self.assertFalse(ok)
        self.assertIn("questions", reason)

    def test_cannot_rewrite_options(self):
        ok, _, reason = admit(QUESTIONS, {"answers": {}, "options": []})
        self.assertFalse(ok)
        self.assertIn("options", reason)

    def test_oversized_answer_rejected(self):
        ok, _, reason = admit(QUESTIONS, {"answers": {"q1": "x" * (MAX_STRING + 1)}})
        self.assertFalse(ok)
        self.assertIn("q1", reason)

    def test_answers_total_budget(self):
        big = "x" * (MAX_STRING - 1)
        ok, _, reason = admit(
            QUESTIONS, {"answers": {"q1": big, "q2": big}}
        )
        self.assertFalse(ok)

    def test_non_string_answer_rejected(self):
        ok, _, reason = admit(QUESTIONS, {"answers": {"q1": {"nested": "obj"}}})
        self.assertFalse(ok)
        self.assertIn("q1", reason)

    def test_list_of_non_strings_rejected(self):
        ok, _, reason = admit(QUESTIONS, {"answers": {"q1": [1, 2, 3]}})
        self.assertFalse(ok)

    def test_answers_not_a_dict_rejected(self):
        ok, _, reason = admit(QUESTIONS, {"answers": "postgres"})
        self.assertFalse(ok)

    def test_empty_payload_rejected(self):
        """什么都不答、也不回应，等同没提交。"""
        ok, _, reason = admit(QUESTIONS, {})
        self.assertFalse(ok)


class FinalizeTest(unittest.TestCase):
    def test_finalize_returns_answers_verbatim(self):
        payload = {"answers": {"q1": "postgres"}}
        out = finalize(QUESTIONS, payload)
        self.assertEqual({"q1": "postgres"}, out["answers"])

    def test_finalize_drops_unanswered_keys(self):
        """空串不算答过 —— 对齐 CC 的 `!!qp(...)`（调研 §9.7.5）。"""
        payload = {"answers": {"q1": "", "q2": "eu"}}
        out = finalize(QUESTIONS, payload)
        self.assertEqual({"q2": "eu"}, out["answers"])

    def test_finalize_raises_on_tampered_payload(self):
        """finalize 是第二道校验：绕过 admit 也过不去。"""
        with self.assertRaises(Exception):
            finalize(QUESTIONS, {"answers": {"q_not_asked": "x"}})


class RefusalPromptTest(unittest.TestCase):
    def test_refusal_prompt_mentions_the_reason(self):
        text = refusal_prompt("unknown answer key: q99")
        self.assertIn("q99", text)

    def test_refusal_prompt_tells_model_not_to_retry_blindly(self):
        text = refusal_prompt("anything")
        self.assertIn("ask_user_question", text)


if __name__ == "__main__":
    unittest.main()
