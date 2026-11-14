"""DANGER 提示该指向哪个提问工具。

方案 §8.3：`ask_user` 会结束回合，模型下一轮还要重新拿上下文；
`ask_user_question` 在本轮内拿到答案，正是「汇报进度并确认是否继续」的用途。

但这里有一个前提：新工具必须是**真的能用**。所以在切换之前，
本文件同时锁住两件事：
1. 提示里提到的是 `ask_user_question`；
2. 该工具确实已注册在 schema 里（否则模型会调用一个不存在的工具）。
"""

from __future__ import annotations

import json
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


class DangerPromptPointsToNewToolTest(unittest.TestCase):
    def test_repeated_turn_danger_uses_ask_user_question(self):
        src = (REPO / "genericagent" / "core" / "handler.py").read_text(encoding="utf-8")
        # 「已连续执行第 N 轮」那条已经切到新工具
        marker = "已连续执行第"
        for line in src.split("\n"):
            if marker in line and "DANGER" in line and "ask_user" in line:
                self.assertIn(
                    "ask_user_question",
                    line,
                    f"这条 DANGER 仍在提示旧工具：{line.strip()[:120]}",
                )
                return
        self.fail("没有找到「已连续执行」这条 DANGER 提示")

    def test_plan_limit_danger_uses_ask_user_question(self):
        src = (REPO / "genericagent" / "core" / "handler.py").read_text(encoding="utf-8")
        marker = "Plan模式已运行"
        for line in src.split("\n"):
            if marker in line and "DANGER" in line:
                self.assertIn(
                    "ask_user_question",
                    line,
                    f"这条 DANGER 仍在提示旧工具：{line.strip()[:120]}",
                )
                return
        self.fail("没有找到 Plan 上限这条 DANGER 提示")


class ToolIsActuallyAvailableTest(unittest.TestCase):
    """前提检查：切换提示词之前，工具必须真的存在。"""

    def test_ask_user_question_is_in_both_schemas(self):
        for name in ("tools_schema.json", "tools_schema_cn.json"):
            for root in (REPO / "assets", REPO / "genericagent" / "resources" / "assets"):
                tools = json.loads((root / name).read_text(encoding="utf-8"))
                names = {t["function"]["name"] for t in tools}
                self.assertIn(
                    "ask_user_question",
                    names,
                    f"{root / name} 里没有 ask_user_question",
                )


if __name__ == "__main__":
    unittest.main()
