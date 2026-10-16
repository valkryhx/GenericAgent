import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from genericagent.core.loop import exhaust
from genericagent.core.handler import GenericAgentHandler


class _ParentStub:
    task_dir = ""
    verbose = False

    def __init__(self):
        self.llmclient = SimpleNamespace(backend=SimpleNamespace(history=[]))


class ChildPathRecoveryTest(unittest.TestCase):
    """A bad workspace path in a child tool call must surface as a recoverable
    tool error, never as a fatal job failure.

    Regression: a verifier called file_read on the workspace root itself; the
    path resolver raised WorkspacePathError out of do_file_read (which, unlike
    file_write/file_patch, had no guard), and the whole verify job failed with
    "path must name a workspace child".
    """

    def _handler(self, workspace: Path) -> GenericAgentHandler:
        return GenericAgentHandler(
            _ParentStub(),
            last_history=[],
            cwd=str(workspace),
            workspace_root=str(workspace),
        )

    @staticmethod
    def _response():
        return SimpleNamespace(content="")

    def test_file_read_on_workspace_root_is_a_recoverable_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            (workspace / "eight_queens.py").write_text("print(92)" + chr(10), encoding="utf-8")
            handler = self._handler(workspace)

            outcome = exhaust(handler.dispatch("file_read", {"path": str(workspace)}, self._response()))

            self.assertIsInstance(outcome.data, dict)
            self.assertEqual("error", outcome.data["status"])
            self.assertIn("invalid workspace path", outcome.data["msg"])

    def test_file_read_on_relative_child_still_reads(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "ws"
            workspace.mkdir()
            (workspace / "eight_queens.py").write_text("print(92)" + chr(10), encoding="utf-8")
            handler = self._handler(workspace)

            outcome = exhaust(handler.dispatch("file_read", {"path": "eight_queens.py"}, self._response()))

            self.assertIn("print(92)", str(outcome.data))


if __name__ == "__main__":
    unittest.main()
