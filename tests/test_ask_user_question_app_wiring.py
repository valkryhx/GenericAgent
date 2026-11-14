"""app 层把 question_runtime 挂到 agent 与 handler 上。

三个接入点，与 permission_runtime 完全对称（实施方案 §4.2/§4.3）：
1. `GenericAgent.__init__` 创建
2. 构造 handler 时注入 `handler.question_runtime`
3. `/stop` / cancel 时 `cancel_all()`

漏掉第 2 步，`do_ask_user_question` 会永远拿到 None → 永远返回 unavailable。
漏掉第 3 步，用户按 /stop 后卡片仍挂在那里等一个永不到来的答案。

这类「接线漏了」的缺陷不会让任何单元测试变红——它们只测各自的函数。
所以这里用 AST 按函数作用域定位接线点（AGENTS.md §8.5 的静态守卫要求：
不要用纯文本匹配，注释里合法提到旧写法会让它因错误原因失败）。
"""

from __future__ import annotations

import ast
import unittest
from pathlib import Path

from genericagent.core.ask_user_question.runtime import QuestionRuntime


REPO = Path(__file__).resolve().parents[1]


def _method_source(path: Path, class_name: str, method_name: str) -> str:
    """取出 `class_name.method_name` 的源码片段。

    必须限定在类作用域内——app.py 模块级也有一个 `__init__` 命名的东西时，
    裸 ast.walk 会先命中它，断言就会因错误的原因失败。
    """
    text = path.read_text(encoding="utf-8")
    tree = ast.parse(text)
    lines = text.split("\n")
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            for item in node.body:
                if (
                    isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and item.name == method_name
                ):
                    return "\n".join(lines[item.lineno - 1 : item.end_lineno])
    return ""


class AppWiringTest(unittest.TestCase):
    def test_init_creates_question_runtime(self):
        src = _method_source(REPO / "genericagent" / "core" / "app.py", "GenericAgent", "__init__")
        self.assertIn("question_runtime", src, "GenericAgent.__init__ 没有创建 question_runtime")
        self.assertIn("QuestionRuntime", src)

    def test_handler_gets_question_runtime(self):
        """handler 构造点必须把 runtime 传下去，否则工具永远 unavailable。

        断言必须精确到 `handler.question_runtime`：
        `GenericAgent.__init__` 里的 `self.question_runtime = ...` 也是
        `*.question_runtime` 赋值，用宽松条件会让这条测试在漏接线时仍然是绿的
        （本项目实测踩过一次，正是 AGENTS.md §8.5 说的假验收）。
        """
        path = REPO / "genericagent" / "core" / "app.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        found = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Assign):
                continue
            for tgt in node.targets:
                if (
                    isinstance(tgt, ast.Attribute)
                    and tgt.attr == "question_runtime"
                    and isinstance(tgt.value, ast.Name)
                    and tgt.value.id == "handler"
                ):
                    found = True
        self.assertTrue(
            found, "没有 `handler.question_runtime = ...`：handler 拿不到 runtime，工具永远 unavailable"
        )

    def test_stop_cancels_pending_questions(self):
        """`/stop` 必须把挂起的提问收尾，否则卡片一直等。

        断言的是「同一个函数体内既有 question_runtime 又有 cancel_all」，
        而不是全文里各自出现过一次——后者会被两句无关代码蒙混过去。
        """
        text = (REPO / "genericagent" / "core" / "app.py").read_text(encoding="utf-8")
        tree = ast.parse(text)
        lines = text.split("\n")
        hit = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = "\n".join(lines[node.lineno - 1 : node.end_lineno])
            if "question_runtime" in body and "cancel_all" in body:
                hit = True
                break
        self.assertTrue(
            hit, "没有任何函数同时操作 question_runtime 与 cancel_all：/stop 不会收拾挂起的提问"
        )


class RuntimeCancelAllTest(unittest.TestCase):
    def test_cancel_all_settles_pending_questions(self):
        import threading
        import time

        rt = QuestionRuntime()
        rt.set_emit(lambda ev: None)
        box: dict = {}

        def worker():
            box["result"] = rt.ask([{"key": "q1", "question": "?"}], timeout_s=5.0)

        t = threading.Thread(target=worker)
        t.start()
        for _ in range(100):
            if rt.pending_ids():
                break
            time.sleep(0.01)

        self.assertEqual(1, rt.cancel_all())
        t.join(timeout=5.0)
        self.assertEqual("cancelled", box["result"].status)


if __name__ == "__main__":
    unittest.main()
