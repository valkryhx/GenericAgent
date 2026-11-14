"""系统提示词：什么时候该问用户。

调研 §11.8 的结论是「没有分类器，全靠提示词判据」。所以提示词里必须
有可操作的判据，否则模型会把「我不确定」当成「该问」。

CC 的两条判据（`eCr` 的「# Delivering work」段，`m0591:110032`）比
工具提示词里那句抽象的「真正属于用户的决定」好落地得多：

1. 不同解读是否会导致**实质上不同**的工作
2. 是否「任何假设下继续都不安全 / 会白做」

GA 的 sys_prompt 目前没有等价段落 —— 这是独立于新工具的改进，
即使不做 ask_user_question 也值得加。
"""

from __future__ import annotations

import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
ASSETS = REPO / "genericagent" / "resources" / "assets"
REPO_ASSETS = REPO / "assets"


class WhenToAskTest(unittest.TestCase):
    def test_english_prompt_states_the_criterion(self):
        text = (ASSETS / "sys_prompt_en.txt").read_text(encoding="utf-8")
        self.assertIn("materially different", text)

    def test_english_prompt_says_do_the_non_blocked_part_first(self):
        text = (ASSETS / "sys_prompt_en.txt").read_text(encoding="utf-8")
        self.assertIn("doesn't depend on the answer", text)

    def test_english_prompt_reserves_blocking_questions(self):
        text = (ASSETS / "sys_prompt_en.txt").read_text(encoding="utf-8")
        self.assertIn("blocking question", text)

    def test_chinese_prompt_has_the_same_criteria(self):
        text = (ASSETS / "sys_prompt.txt").read_text(encoding="utf-8")
        self.assertIn("实质", text)
        self.assertIn("阻塞", text)

    def test_both_copies_stay_in_sync(self):
        """assets/ 与 resources/assets/ 必须一致（见 test_runtime_asset_refresh）。"""
        for name in ("sys_prompt.txt", "sys_prompt_en.txt"):
            a = (REPO_ASSETS / name).read_bytes()
            b = (ASSETS / name).read_bytes()
            self.assertEqual(a, b, f"{name} 两份副本不一致")


class UnattendedSuppressionTest(unittest.TestCase):
    """无人值守时要说「没人会回答」，而不是让模型白问一次（调研 §11.8.3）。"""

    def test_english_prompt_has_unattended_note(self):
        text = (ASSETS / "sys_prompt_en.txt").read_text(encoding="utf-8")
        self.assertIn("running unattended", text)

    def test_chinese_prompt_has_unattended_note(self):
        text = (ASSETS / "sys_prompt.txt").read_text(encoding="utf-8")
        self.assertIn("无人值守", text)


if __name__ == "__main__":
    unittest.main()
