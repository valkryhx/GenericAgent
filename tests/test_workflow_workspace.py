from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from workflow_workspace import (
    WorkspacePathError,
    normalize_declared_artifact_path,
    normalize_workspace_relative,
    resolve_workspace_child,
    workspace_metadata,
)


class WorkflowWorkspaceTest(unittest.TestCase):
    def test_relative_path_resolves_under_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual("tmp/report.html", normalize_workspace_relative("tmp/report.html", root))
            self.assertEqual(root / "tmp" / "report.html", resolve_workspace_child("tmp/report.html", root))

    def test_outside_absolute_paths_are_rejected(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for raw in ("/tmp/report.html", r"D:\tmp\report.html", "../report.html"):
                with self.subTest(raw=raw):
                    with self.assertRaises(WorkspacePathError):
                        normalize_workspace_relative(raw, root)

    def test_legacy_tmp_artifact_is_mapped_inside_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.assertEqual("tmp/report.html", normalize_declared_artifact_path("/tmp/report.html", root))

    def test_absolute_path_inside_root_is_normalized(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            absolute = root / "tmp" / "report.html"
            self.assertEqual("tmp/report.html", normalize_workspace_relative(absolute, root))

    def test_metadata_records_canonical_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = workspace_metadata(Path(tmp))
            self.assertEqual(str(Path(tmp).resolve()), metadata["workspacePath"])
            self.assertEqual("cwd-rooted-workspace-write-v1", metadata["workspacePolicy"])


if __name__ == "__main__":
    unittest.main()
