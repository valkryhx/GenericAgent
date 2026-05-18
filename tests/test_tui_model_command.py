import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTENDS = REPO_ROOT / "frontends"
for path in (str(REPO_ROOT), str(FRONTENDS)):
    if path not in sys.path:
        sys.path.insert(0, path)

from tuiapp import AgentSession as TuiSession  # noqa: E402
from tuiapp import GenericAgentTUI as TuiApp  # noqa: E402
from tuiapp_v2 import COMMANDS as V2_COMMANDS  # noqa: E402
from tuiapp_v2 import AgentSession as TuiV2Session  # noqa: E402
from tuiapp_v2 import GenericAgentTUI as TuiAppV2  # noqa: E402


class FakeAgent:
    def __init__(self):
        self.selected = None

    def select_llm(self, selector):
        self.selected = selector
        return {"ok": True, "index": 1, "name": "NativeOAISession/kimi-native"}

    def list_llms(self):
        return [(0, "NativeOAISession/gpt-native", False), (1, "NativeOAISession/kimi-native", True)]


class TuiModelCommandTests(unittest.TestCase):
    def test_tui_dispatches_model_to_llm_handler(self):
        app = TuiApp.__new__(TuiApp)
        calls = []
        app._cmd_llm = lambda args: calls.append(args)

        app._dispatch_command("model", ["kimi"])

        self.assertEqual(calls, [["kimi"]])

    def test_tui_v2_handlers_include_model_alias(self):
        app = TuiAppV2.__new__(TuiAppV2)
        app._cmd_llm = lambda args: args

        handlers = app._handlers()

        self.assertIs(handlers["model"], app._cmd_llm)

    def test_tui_v2_command_palette_lists_model(self):
        command_names = [name for name, _args, _desc in V2_COMMANDS]

        self.assertIn("/model", command_names)

    def test_tui_llm_command_uses_selector_api(self):
        app = TuiApp.__new__(TuiApp)
        agent = FakeAgent()
        app.current_id = 1
        app.sessions = {1: TuiSession(agent_id=1, name="main", agent=agent)}
        messages = []
        app._system = lambda text: messages.append(text)

        app._cmd_llm(["kimi"])

        self.assertEqual(agent.selected, "kimi")
        self.assertTrue(any("Switched model" in message for message in messages))

    def test_tui_v2_llm_command_uses_selector_api(self):
        app = TuiAppV2.__new__(TuiAppV2)
        agent = FakeAgent()
        app.current_id = 1
        app.sessions = {1: TuiV2Session(agent_id=1, name="main", agent=agent)}
        messages = []
        app._system = lambda text: messages.append(text)

        app._cmd_llm(["kimi"])

        self.assertEqual(agent.selected, "kimi")
        self.assertTrue(any("Switched model" in message or "已切换" in message for message in messages))


if __name__ == "__main__":
    unittest.main()
