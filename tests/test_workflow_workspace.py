from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from workflow_workspace import (
    WorkspacePathError,
    create_run_workspace,
    observed_artifact_owners,
    observed_artifact_paths,
    workspace_writes_with_writer,
    default_workspace_root,
    diff_workspace,
    normalize_declared_artifact_path,
    normalize_workspace_relative,
    run_workspace_path,
    resolve_workspace_child,
    resolve_workspace_root,
    snapshot_workspace,
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

    def test_default_workspace_is_the_project_temp_dir_not_the_launch_cwd(self):
        """Artifacts must not land in GA's own source tree.

        A normal ``ga`` launch starts with cwd == the repository root, so the old
        cwd-derived workspace put a finished report next to agentmain.py. The
        default is the gitignored project temp/ directory instead.
        """
        import os

        repo_root = Path(__file__).resolve().parents[1]
        expected = (repo_root / "temp").resolve()
        with patch.dict(os.environ, {"GA_WORKFLOW_WORKSPACE_ROOT": ""}, clear=False):
            self.assertEqual(expected, default_workspace_root().resolve())

    def test_explicit_env_override_wins_for_workspace_default(self):
        import os

        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"GA_WORKFLOW_WORKSPACE_ROOT": tmp}, clear=False):
                self.assertEqual(Path(tmp).resolve(), default_workspace_root().resolve())
                self.assertEqual(Path(tmp).resolve(), resolve_workspace_root().resolve())

    def test_metadata_records_canonical_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            metadata = workspace_metadata(Path(tmp))
            self.assertEqual(str(Path(tmp).resolve()), metadata["workspacePath"])
            self.assertEqual("project-temp-workspace-write-v1", metadata["workspacePolicy"])

    def test_run_workspace_is_a_sibling_directory_under_the_base_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            path = run_workspace_path(root, "wf_abc")

            self.assertEqual(root / "workflow-runs" / "wf_abc", path)
            self.assertFalse(path.exists())

            created = create_run_workspace(root, "wf_abc")
            self.assertTrue(created.is_dir())
            self.assertEqual(path, created)

    def test_run_workspace_rejects_path_segments_in_the_run_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for run_id in ("", "  ", ".", "..", "../escape", "a/b", r"a\b"):
                with self.subTest(run_id=run_id):
                    with self.assertRaises(WorkspacePathError):
                        run_workspace_path(root, run_id)

    def test_snapshot_diff_detects_created_and_modified_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "keep.txt").write_text("keep", encoding="utf-8")
            (root / "nested").mkdir()
            (root / "nested" / "old.txt").write_text("old", encoding="utf-8")
            before = snapshot_workspace(root)

            (root / "nested" / "old.txt").write_text("changed and longer", encoding="utf-8")
            (root / "new.md").write_text("new", encoding="utf-8")

            self.assertEqual(["nested/old.txt", "new.md"], diff_workspace(before, snapshot_workspace(root)))

    def test_snapshot_ignores_pycache_and_diff_is_empty_without_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.txt").write_text("a", encoding="utf-8")
            (root / "__pycache__").mkdir()
            (root / "__pycache__" / "junk.pyc").write_bytes(b"junk")
            before = snapshot_workspace(root)

            self.assertEqual(["a.txt"], sorted(before))
            self.assertEqual([], diff_workspace(before, snapshot_workspace(root)))

    def test_snapshot_of_missing_directory_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual({}, snapshot_workspace(Path(tmp) / "does-not-exist"))



class ObservedArtifactShapeTest(unittest.TestCase):
    """``observedArtifacts`` carries ownership, and both shapes keep loading.

    The path alone answers "what did the run produce"; the writer answers "which
    job produced it", which matters when two children write the same name. The
    writer is recorded at diff time, never re-derived from tool names.
    """

    def test_bare_paths_still_load_as_entries_without_a_writer(self):
        entries = workspace_writes_with_writer(["a.md", "  ", None, "b/c.md"])

        self.assertEqual(
            [{"path": "a.md", "writer": ""}, {"path": "b/c.md", "writer": ""}],
            entries,
        )
        self.assertEqual(["a.md", "b/c.md"], observed_artifact_paths(entries))

    def test_dict_entries_keep_their_writer_and_drop_empty_paths(self):
        entries = workspace_writes_with_writer([
            {"path": "a.md", "writer": "synthesis"},
            {"path": "", "writer": "ignored"},
            {"path": "b.md"},
        ])

        self.assertEqual(
            [{"path": "a.md", "writer": "synthesis"}, {"path": "b.md", "writer": ""}],
            entries,
        )
        self.assertEqual(["a.md", "b.md"], observed_artifact_paths(entries))

    def test_owners_map_lists_every_writer_for_a_shared_path(self):
        owners = observed_artifact_owners([
            {"path": "report.md", "writer": "research"},
            {"path": "report.md", "writer": "synthesis"},
            {"path": "research_notes.md", "writer": "research"},
            {"path": "orphan.md", "writer": ""},
        ])

        self.assertEqual(
            {"report.md": ["research", "synthesis"], "research_notes.md": ["research"]},
            owners,
        )
        self.assertNotIn("orphan.md", owners)

    def test_mixed_legacy_and_owner_entries_are_both_understood(self):
        entries = workspace_writes_with_writer(["legacy.md", {"path": "new.md", "writer": "job_2"}])

        self.assertEqual(["legacy.md", "new.md"], observed_artifact_paths(entries))
        self.assertEqual({"new.md": ["job_2"]}, observed_artifact_owners(entries))

if __name__ == "__main__":
    unittest.main()
