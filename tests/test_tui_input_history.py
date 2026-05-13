import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTENDS = REPO_ROOT / "frontends"
for path in (str(REPO_ROOT), str(FRONTENDS)):
    if path not in sys.path:
        sys.path.insert(0, path)

for name in ("agentmain", "chatapp_common", "continue_cmd", "llmcore"):
    module = sys.modules.get(name)
    if module is not None and not getattr(module, "__file__", None):
        sys.modules.pop(name)

from tuiapp import PromptInput  # noqa: E402
from tuiapp_v2 import InputArea  # noqa: E402


class InputHistoryTest(unittest.TestCase):
    def assert_history_navigation(self, input_cls):
        inp = input_cls()
        inp.add_history("first")
        inp.add_history("second")

        inp.text = "draft"
        self.assertTrue(inp.show_previous_history())
        self.assertEqual(inp.text, "second")
        self.assertTrue(inp.show_previous_history())
        self.assertEqual(inp.text, "first")
        self.assertTrue(inp.show_previous_history())
        self.assertEqual(inp.text, "first")

        self.assertTrue(inp.show_next_history())
        self.assertEqual(inp.text, "second")
        self.assertTrue(inp.show_next_history())
        self.assertEqual(inp.text, "draft")
        self.assertFalse(inp.show_next_history())

    def test_prompt_input_history_navigation_restores_draft(self):
        self.assert_history_navigation(PromptInput)

    def test_input_area_history_navigation_restores_draft(self):
        self.assert_history_navigation(InputArea)

    def test_input_history_ignores_blank_and_consecutive_duplicates(self):
        inp = InputArea()
        inp.add_history("")
        inp.add_history("same")
        inp.add_history("same")
        inp.add_history("next")

        self.assertEqual(inp._history, ["same", "next"])
