import copy
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from genericagent.llm.core import ClaudeSession, NativeClaudeSession  # noqa: E402


CFG = {
    "apikey": "sk-test-not-a-real-key",
    "apibase": "https://example.test/v1",
    "model": "claude-opus-5",
    "stream": True,
    "auto_compact_enabled": True,
    "context_win": 400_000,
}

OLD_HISTORY = []
for index in range(4):
    OLD_HISTORY.extend([
        {"role": "user", "content": [{"type": "text", "text": f"old-{index}-" + "x" * 4000}]},
        {"role": "assistant", "content": [{"type": "text", "text": f"old-answer-{index}-" + "y" * 4000}]},
    ])


class ProjectionTrimTest(unittest.TestCase):
    def _capture(self, session):
        captured = {}

        def fake_raw_ask(messages):
            captured["messages"] = messages
            yield ""
            return [{"type": "text", "text": "ok"}]

        session.raw_ask = fake_raw_ask
        return captured

    def test_claude_session_hard_trim_keeps_canonical_history_and_sends_trimmed_copy(self):
        session = ClaudeSession(dict(CFG))
        session.history = copy.deepcopy(OLD_HISTORY)
        session.hard_limit_tokens = 100
        before = copy.deepcopy(session.history)
        captured = self._capture(session)

        list(session.ask("new question"))

        self.assertEqual(before, session.history[:len(before)])
        self.assertEqual("new question", session.history[-1]["content"][0]["text"])
        self.assertLess(len(captured["messages"]), len(session.history))
        self.assertEqual("user", captured["messages"][0]["role"])

    def test_native_session_hard_trim_keeps_canonical_history_and_sends_trimmed_copy(self):
        session = NativeClaudeSession(dict(CFG))
        session.history = copy.deepcopy(OLD_HISTORY)
        session.hard_limit_tokens = 100
        before = copy.deepcopy(session.history)
        captured = self._capture(session)

        list(session.ask({"role": "user", "content": [{"type": "text", "text": "new question"}]}))

        self.assertEqual(before, session.history[:len(before)])
        self.assertEqual("new question", session.history[-2]["content"][0]["text"])
        self.assertEqual("ok", session.history[-1]["content"][0]["text"])
        self.assertLess(len(captured["messages"]), len(session.history))
        self.assertEqual("user", captured["messages"][0]["role"])


if __name__ == "__main__":
    unittest.main()
