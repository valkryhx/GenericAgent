import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from genericagent.core.handler import GenericAgentHandler, file_patch
from genericagent.core.loop import exhaust


class FileToolNewlineTest(unittest.TestCase):
    """file_write / file_patch must not inject CRLF on Windows (PEP text mode would)."""

    def make_handler(self, td):
        parent = SimpleNamespace(task_dir=str(Path(td) / "temp"), permission_mode="full_access")
        Path(td, "temp").mkdir(exist_ok=True)
        Path(td, "workspace").mkdir(exist_ok=True)
        return GenericAgentHandler(parent, last_history=[], cwd=str(Path(td) / "workspace"),
                                   workspace_root=str(Path(td) / "workspace"))

    def test_file_write_preserves_lf(self):
        with tempfile.TemporaryDirectory() as td:
            handler = self.make_handler(td)
            target = Path(td) / "workspace" / "out.txt"
            exhaust(handler.dispatch("file_write",
                                     {"path": str(target), "content": "a\nb\nc"}, None))
            raw = target.read_bytes()
            self.assertEqual(b"a\nb\nc", raw)
            self.assertNotIn(b"\r", raw)

    def test_file_write_append_preserves_lf(self):
        with tempfile.TemporaryDirectory() as td:
            handler = self.make_handler(td)
            target = Path(td) / "workspace" / "app.txt"
            exhaust(handler.dispatch("file_write", {"path": str(target), "content": "x\n"}, None))
            exhaust(handler.dispatch("file_write",
                                     {"path": str(target), "content": "y\n", "mode": "append"}, None))
            raw = target.read_bytes()
            self.assertEqual(b"x\ny\n", raw)
            self.assertNotIn(b"\r", raw)

    def test_file_patch_preserves_lf(self):
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "p.txt"
            target.write_bytes(b"line1\nline2\nline3\n")
            result = file_patch(str(target), "line2\n", "LINE2\n")
            self.assertEqual(result.get("status"), "success")
            raw = target.read_bytes()
            self.assertEqual(b"line1\nLINE2\nline3\n", raw)
            self.assertNotIn(b"\r", raw)


if __name__ == "__main__":
    unittest.main()
