import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
import sys

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from tool_result_budget import budget_tool_result  # noqa: E402
from tool_result_limits import (  # noqa: E402
    clear_tool_result_max_chars,
    get_tool_result_max_chars,
    replace_tool_result_max_chars,
    set_tool_result_max_chars,
)
from agent_loop import StepOutcome, agent_runner_loop  # noqa: E402


class _ToolCall:
    def __init__(self, name, args, call_id):
        self.function = SimpleNamespace(name=name, arguments=json.dumps(args))
        self.id = call_id


class _Response:
    def __init__(self, tool_calls=None):
        self.tool_calls = tool_calls or []
        self.content = ""


class _Client:
    def __init__(self, response):
        self.response = response
        self.last_tools = ""

    def chat(self, messages=None, tools=None):
        if False:
            yield ""
        return self.response


class _LoopHandler:
    max_turns = 1

    def __init__(self, root):
        self.parent = SimpleNamespace(task_dir=None)
        self.workspace_root = str(root)
        self.cwd = str(root)
        self.tool_result_namespace = "loop/run"
        self._done_hooks = []
        self.current_turn = 0
        self.results = []

    def dispatch(self, tool_name, args, response, index=0, tool_num=1):
        yield "tool output\n"
        return StepOutcome({"blob": "x" * 80_000}, next_prompt="\n")

    def turn_end_callback(self, response, tool_calls, tool_results, turn, next_prompt, exit_reason):
        self.results.extend(tool_results)
        return next_prompt


class ToolResultBudgetTest(unittest.TestCase):
    def setUp(self):
        clear_tool_result_max_chars()

    def tearDown(self):
        clear_tool_result_max_chars()

    def _handler(self, root):
        return SimpleNamespace(
            workspace_root=str(root),
            cwd=str(root),
            tool_result_namespace="run-1/agent-1",
        )

    def test_small_result_is_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            result = budget_tool_result(self._handler(tmp), "mcp__demo__read", "t1", "small")
        self.assertEqual(result, "small")

    def test_large_text_is_persisted_with_preview_and_readable_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            content = "alpha\n" + ("x" * 80_000)
            result = budget_tool_result(self._handler(root), "mcp__demo__read", "t1", content)
            self.assertIn("<persisted-output>", result)
            self.assertIn("artifactPath: tool-results/run-1/agent-1/t1.txt", result)
            self.assertIn("originalChars:", result)
            self.assertIn("truncated: false", result)
            saved = root / "tool-results" / "run-1" / "agent-1" / "t1.txt"
            self.assertTrue(saved.is_file())
            self.assertEqual(content, saved.read_text(encoding="utf-8"))
            self.assertIn("alpha", result)

    def test_large_structured_result_uses_valid_projection(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            payload = {"items": [{"id": i, "text": "x" * 100} for i in range(800)]}
            content = json.dumps(payload, ensure_ascii=False)
            result = budget_tool_result(
                self._handler(root),
                "mcp__demo__read",
                "structured",
                content,
                structured=True,
            )
            self.assertIn("<persisted-output>", result)
            saved = root / "tool-results" / "run-1" / "agent-1" / "structured.json"
            self.assertEqual(payload, json.loads(saved.read_text(encoding="utf-8")))
            self.assertNotIn("...", result.split("Preview:", 1)[1])

    def test_persist_failure_falls_back_to_bounded_truncation(self):
        import tool_result_budget
        from unittest import mock

        with tempfile.TemporaryDirectory() as tmp:
            handler = self._handler(tmp)
            with mock.patch.object(tool_result_budget, "_write_persisted", side_effect=OSError("disk full")):
                result = budget_tool_result(handler, "mcp__demo__read", "t1", "z" * 80_000)
        self.assertIn("<truncated-output>", result)
        self.assertIn("could not be saved", result)
        self.assertIn("originalChars:", result)
        self.assertIn("truncated: true", result)
        self.assertLess(len(result), 80_000)

    def test_self_bounded_tools_are_exempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            content = "x" * 80_000
            result = budget_tool_result(self._handler(tmp), "file_read", "t1", content)
        self.assertEqual(result, content)

    def test_registered_per_tool_limit_overrides_global_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            set_tool_result_max_chars("mcp__demo__read", 20)
            result = budget_tool_result(
                self._handler(tmp),
                "mcp__demo__read",
                "t1",
                "x" * 80_000,
            )
        self.assertIn("<persisted-output>", result)
        self.assertIn("originalChars: 80000", result)

    def test_explicit_per_tool_limit_wins_over_registered_limit(self):
        with tempfile.TemporaryDirectory() as tmp:
            set_tool_result_max_chars("mcp__demo__read", 80_000)
            result = budget_tool_result(
                self._handler(tmp),
                "mcp__demo__read",
                "t1",
                "x" * 100,
                max_chars=20,
            )
        self.assertIn("<persisted-output>", result)

    def test_registered_limit_can_opt_exempt_tool_into_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            set_tool_result_max_chars("file_read", 20)
            result = budget_tool_result(
                self._handler(tmp),
                "file_read",
                "t1",
                "x" * 80_000,
            )
        self.assertIn("<persisted-output>", result)

    def test_replace_with_prefix_preserves_other_tool_limits(self):
        set_tool_result_max_chars("mcp__a__read", 10)
        set_tool_result_max_chars("mcp__b__read", 30)
        replace_tool_result_max_chars({"mcp__b__read": 20}, prefix="mcp__b__")
        self.assertEqual(get_tool_result_max_chars("mcp__a__read"), 10)
        self.assertEqual(get_tool_result_max_chars("mcp__b__read"), 20)

    def test_invalid_registered_limit_falls_back_to_global(self):
        with tempfile.TemporaryDirectory() as tmp:
            set_tool_result_max_chars("mcp__demo__read", 0)
            result = budget_tool_result(
                self._handler(tmp),
                "mcp__demo__read",
                "t1",
                "x" * 80_000,
            )
        self.assertIn("<persisted-output>", result)

    def test_agent_loop_uses_the_budget_layer(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            handler = _LoopHandler(root)
            client = _Client(_Response([_ToolCall("mcp__demo__read", {}, "call-1")]))
            list(agent_runner_loop(client, "sys", "go", handler, [], max_turns=1, verbose=False))
            self.assertEqual(1, len(handler.results))
            self.assertIn("<persisted-output>", handler.results[0]["content"])
            self.assertTrue((root / "tool-results" / "loop" / "run" / "call-1.json").is_file())


if __name__ == "__main__":
    unittest.main()
