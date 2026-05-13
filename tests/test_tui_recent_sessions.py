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


from tuiapp_v2 import (  # noqa: E402
    AgentSession,
    _recent_sidebar_sessions,
    _session_rows,
    _tui_recent_sessions_limit,
)


class TUIRecentSessionsTest(unittest.TestCase):
    def test_recent_limit_defaults_and_clamps_invalid_values(self):
        self.assertEqual(_tui_recent_sessions_limit({}), 10)
        self.assertEqual(_tui_recent_sessions_limit({"tui_recent_sessions_limit": "3"}), 3)
        self.assertEqual(_tui_recent_sessions_limit({"tui_recent_sessions_limit": 0}), 10)
        self.assertEqual(_tui_recent_sessions_limit({"tui_recent_sessions_limit": "bad"}), 10)

    def test_recent_sidebar_sessions_respects_limit(self):
        sessions = [(f"path-{i}", 100 - i, f"preview-{i}", i + 1) for i in range(12)]

        recent = _recent_sidebar_sessions(sessions, 10)

        self.assertEqual(len(recent), 10)
        self.assertEqual(recent[0][0], "path-0")
        self.assertEqual(recent[-1][0], "path-9")

    def test_session_rows_include_preview_lines(self):
        sess = AgentSession(agent_id=1, name="main", agent=object())

        self.assertEqual(_session_rows(sess), 3)

        sess.messages.append(type("Msg", (), {"role": "user", "content": "hello"})())
        self.assertEqual(_session_rows(sess), 4)


if __name__ == "__main__":
    unittest.main()
