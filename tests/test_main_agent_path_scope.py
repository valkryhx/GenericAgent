"""Main agent must not be confined to project_root.

Regression for: the interactive main agent's handler was constructed with
``workspace_root=project_root`` (a workflow-child hard-sandbox concept), which
(a) made the PathACL deny any absolute path outside the project and (b) turned
``code_run`` into Python-only workspace-guard mode. User-specified absolute
paths must be reachable from the main agent.
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from genericagent.core.app import build_main_handler  # noqa: E402


class _Parent:
    def __init__(self, project_root):
        self.project_root = str(project_root)
        self.data_root = str(Path(project_root) / "data")
        self.history = []

    class llmclient:  # minimal shape touched by handler init
        class backend:
            history = []

    def __getattr__(self, name):
        return None


class MainAgentPathScopeTest(unittest.TestCase):
    def test_main_handler_has_no_workspace_root(self):
        with tempfile.TemporaryDirectory() as td:
            parent = _Parent(td)
            handler = build_main_handler(parent, [], td)
            self.assertIsNone(handler.workspace_root)

    def test_main_handler_can_resolve_absolute_path_outside_project(self):
        with tempfile.TemporaryDirectory() as project_td, tempfile.TemporaryDirectory() as outside_td:
            parent = _Parent(project_td)
            handler = build_main_handler(parent, [], project_td)
            outside = Path(outside_td) / "deliverable.txt"
            # No WorkspacePathError, and the resolved path is the absolute one.
            resolved = Path(handler._get_abs_path(str(outside))).resolve()
            self.assertEqual(resolved, outside.resolve())
            # PathACL is inactive for the main agent (no workspace_root).
            self.assertIsNone(handler._check_workspace_path_access("file_read", {"path": str(outside)}))
            self.assertIsNone(handler._check_workspace_path_access("file_write", {"path": str(outside)}))

    def test_main_handler_relative_path_anchors_to_tool_cwd(self):
        with tempfile.TemporaryDirectory() as td:
            parent = _Parent(td)
            handler = build_main_handler(parent, [], td)
            resolved = Path(handler._get_abs_path("notes/out.txt")).resolve()
            self.assertEqual(resolved, (Path(td) / "notes" / "out.txt").resolve())


if __name__ == "__main__":
    unittest.main()
