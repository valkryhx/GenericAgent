from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


class WorkflowWorkspaceGuardTest(unittest.TestCase):
    def _run(self, workspace: Path, expression: str):
        code = textwrap.dedent(
            f"""
            import json
            import subprocess
            from pathlib import Path
            from workflow_workspace_guard import install
            install({str(workspace)!r})
            result = {{"ok": False, "error": None}}
            try:
                {expression}
                result["ok"] = True
            except Exception as exc:
                result["error"] = type(exc).__name__ + ": " + str(exc)
            print(json.dumps(result))
            """
        )
        env = dict(__import__("os").environ)
        repo_root = str(Path(__file__).resolve().parents[1])
        env["PYTHONPATH"] = repo_root + __import__("os").pathsep + env.get("PYTHONPATH", "")
        completed = subprocess.run(
            [sys.executable, "-c", code],
            cwd=str(workspace),
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        return json.loads(completed.stdout.strip())

    def test_path_write_text_inside_workspace_is_allowed(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            result = self._run(workspace, "Path('tmp/report.html').parent.mkdir(parents=True); Path('tmp/report.html').write_text('<html/>')")
            self.assertTrue(result["ok"], result)
            self.assertEqual("<html/>", (workspace / "tmp/report.html").read_text())

    def test_path_write_text_outside_workspace_is_denied(self):
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            workspace = Path(tmp)
            target = Path(outside) / "outside.txt"
            result = self._run(workspace, f"Path({str(target)!r}).write_text('blocked')")
            self.assertFalse(result["ok"], result)
            self.assertIn("PermissionError", result["error"])
            self.assertFalse(target.exists())

    def test_parent_traversal_and_subprocess_are_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            traversal = self._run(workspace, "Path('../outside.txt').write_text('blocked')")
            self.assertFalse(traversal["ok"], traversal)
            self.assertIn("PermissionError", traversal["error"])
            process = self._run(workspace, "subprocess.run(['echo', 'blocked'])")
            self.assertFalse(process["ok"], process)
            self.assertIn("PermissionError", process["error"])


if __name__ == "__main__":
    unittest.main()
