import tempfile
import unittest
from pathlib import Path

from workflow_path_acl import (
    EXECUTE,
    READ,
    WRITE,
    check_path_access,
    check_tool_call,
    command_write_targets,
    is_inside_workspace,
)


class WorkflowPathAclTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.workspace = self.base / "ws"
        self.workspace.mkdir()
        (self.workspace / "sub").mkdir()
        (self.workspace / "sub" / "inside.txt").write_text("INSIDE\n", encoding="utf-8")
        (self.base / "secret.txt").write_text("TOP_SECRET\n", encoding="utf-8")

    def test_path_access_allows_inside_and_rejects_outside(self):
        self.assertTrue(check_path_access(self.workspace, "sub/inside.txt", READ).allowed)
        self.assertTrue(check_path_access(self.workspace, "sub/new.txt", WRITE).allowed)
        self.assertFalse(check_path_access(self.workspace, "../secret.txt", READ).allowed)
        self.assertFalse(check_path_access(self.workspace, str(self.base / "secret.txt"), READ).allowed)
        self.assertFalse(check_path_access(self.workspace, "", READ).allowed)

    def test_read_tool_outside_workspace_is_denied(self):
        decision = check_tool_call(self.workspace, "file_read", {"path": "../secret.txt"})

        self.assertFalse(decision.allowed)
        self.assertEqual(READ, decision.operation)

    def test_write_tool_outside_workspace_is_denied(self):
        decision = check_tool_call(self.workspace, "file_write", {"path": "../escape.txt", "content": "x"})

        self.assertFalse(decision.allowed)
        self.assertEqual(WRITE, decision.operation)

    def test_write_tool_reads_file_ref_outside_workspace_is_denied(self):
        """A legal output path must not smuggle an out-of-workspace read."""

        decision = check_tool_call(
            self.workspace,
            "file_write",
            {"path": "out.txt", "content": "{{file:../secret.txt:1:1}}"},
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(READ, decision.operation)

    def test_write_tool_with_inside_file_ref_is_allowed(self):
        decision = check_tool_call(
            self.workspace,
            "file_write",
            {"path": "out.txt", "content": "{{file:sub/inside.txt:1:1}}"},
        )

        self.assertTrue(decision.allowed)

    def test_shell_redirection_outside_workspace_is_denied(self):
        outside = self.base / "escaped.txt"
        decision = check_tool_call(
            self.workspace,
            "code_run",
            {"type": "bash", "code": f"echo PWNED > '{outside}'"},
        )

        self.assertFalse(decision.allowed)
        self.assertEqual(EXECUTE, decision.operation)

    def test_shell_redirection_inside_workspace_is_allowed(self):
        decision = check_tool_call(
            self.workspace,
            "code_run",
            {"type": "bash", "code": "echo ok > sub/out.txt"},
        )

        self.assertTrue(decision.allowed)

    def test_python_code_run_checks_cwd_not_command_text(self):
        allowed = check_tool_call(self.workspace, "code_run", {"type": "python", "code": "print(1)", "cwd": "."})
        denied = check_tool_call(self.workspace, "code_run", {"type": "python", "code": "print(1)", "cwd": "../"})

        self.assertTrue(allowed.allowed)
        self.assertFalse(denied.allowed)

    def test_non_filesystem_and_mcp_tools_are_not_path_restricted(self):
        for tool_name in ("load_skill", "web_scan", "no_tool", "mcp__tavily__tavily_search"):
            self.assertTrue(check_tool_call(self.workspace, tool_name, {"query": "x"}).allowed)

    def test_no_workspace_root_allows_everything(self):
        self.assertTrue(check_tool_call(None, "file_read", {"path": "../secret.txt"}).allowed)

    def test_command_write_targets_extracts_common_forms(self):
        self.assertEqual(["out.txt"], [t for t in command_write_targets("echo hi > out.txt")])
        self.assertEqual(["b.txt"], [t for t in command_write_targets("mv a.txt b.txt")])
        self.assertEqual(["log.txt"], [t for t in command_write_targets("cat x | tee log.txt")])
        self.assertEqual(["img.bin"], [t for t in command_write_targets("dd if=a of=img.bin")])

    def test_symlink_escape_is_rejected(self):
        link = self.workspace / "link"
        try:
            link.symlink_to(self.base, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks are not available in this environment")

        self.assertFalse(is_inside_workspace(self.workspace, "link/secret.txt"))
        self.assertFalse(check_tool_call(self.workspace, "file_read", {"path": "link/secret.txt"}).allowed)


if __name__ == "__main__":
    unittest.main()
