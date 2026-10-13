from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


class WorkflowWorkspaceGuardTest(unittest.TestCase):
    def _run(self, workspace: Path, expression: str, extra_env: dict | None = None):
        code = textwrap.dedent(
            f"""
            import json
            import subprocess
            import sys
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
        env = dict(os.environ)
        repo_root = str(Path(__file__).resolve().parents[1])
        env["PYTHONPATH"] = repo_root + os.pathsep + env.get("PYTHONPATH", "")
        for key in ("GA_WORKFLOW_GUARD_STRICT", "GA_WORKFLOW_BASH_CLAMP"):
            env.pop(key, None)
        if extra_env:
            env.update(extra_env)
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

    def test_parent_traversal_is_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            traversal = self._run(workspace, "Path('../outside.txt').write_text('blocked')")
            self.assertFalse(traversal["ok"], traversal)
            self.assertIn("PermissionError", traversal["error"])

    def test_subprocess_is_allowed_by_default(self):
        # Claude Code lets a workflow child run commands; the guard no longer
        # blocks every subprocess. The command is confined to the workspace cwd.
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            result = self._run(
                workspace,
                "subprocess.run([sys.executable, '-c', 'print(1)'], check=True, capture_output=True)",
            )
            self.assertTrue(result["ok"], result)

    def test_dangerous_subprocess_is_denied(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            result = self._run(workspace, "subprocess.run(['rm', '-rf', '/'])")
            self.assertFalse(result["ok"], result)
            self.assertIn("PermissionError", result["error"])

    def test_strict_mode_blocks_every_subprocess(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            result = self._run(
                workspace,
                "subprocess.run([sys.executable, '-c', 'print(1)'])",
                extra_env={"GA_WORKFLOW_GUARD_STRICT": "1"},
            )
            self.assertFalse(result["ok"], result)
            self.assertIn("PermissionError", result["error"])

    def test_bash_command_clamp_scopes_allowed_forms(self):
        # A clamp admits only matching command forms (fail-closed otherwise).
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            allowed = self._run(
                workspace,
                "subprocess.run(['nonexistent-program-xyz'])",
                extra_env={"GA_WORKFLOW_BASH_CLAMP": json.dumps(["nonexistent-program-xyz"])},
            )
            # The clamp admits the form, so execution proceeds and fails later
            # with a "not found" error rather than a guard PermissionError.
            self.assertNotIn("PermissionError", allowed["error"] or "")
            blocked = self._run(
                workspace,
                "subprocess.run(['other-program-abc'])",
                extra_env={"GA_WORKFLOW_BASH_CLAMP": json.dumps(["nonexistent-program-xyz"])},
            )
            self.assertFalse(blocked["ok"], blocked)
            self.assertIn("PermissionError", blocked["error"])

    def test_bash_command_clamp_rejects_unverifiable_shell_structure(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp)
            result = self._run(
                workspace,
                "subprocess.run('echo hi && echo bye', shell=True)",
                extra_env={"GA_WORKFLOW_BASH_CLAMP": json.dumps(["echo"])},
            )
            self.assertFalse(result["ok"], result)
            self.assertIn("PermissionError", result["error"])


if __name__ == "__main__":
    unittest.main()
