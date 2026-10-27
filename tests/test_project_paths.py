import os
import tempfile
import unicodedata
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest import mock

from genericagent import paths


class ProjectKeyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_project_key_normalizes_windows_case_unicode_and_collisions(self):
        cafe = self.root / unicodedata.normalize("NFC", "Café")
        cafe_decomposed = self.root / unicodedata.normalize("NFD", "Café")
        self.assertEqual(paths.project_key(cafe), paths.project_key(cafe_decomposed))

        if os.name == "nt":
            self.assertEqual(paths.project_key(cafe), paths.project_key(self.root / "CAFÉ"))

        first = self.root / "one" / "same"
        second = self.root / "two" / "same"
        self.assertNotEqual(paths.project_key(first), paths.project_key(second))

    def test_blank_explicit_project_stays_distinct_inside_parent_repo(self):
        parent = self.root / "parent-repo"
        blank = parent / "scratch"
        nested = blank / "src"
        (parent / ".git").mkdir(parents=True)
        nested.mkdir(parents=True)

        self.assertEqual(paths.project_key(parent), paths.project_key(nested))
        self.assertNotEqual(paths.project_key_exact(blank), paths.project_key_exact(parent))
        roots = paths.project_roots(project=blank, data_root=self.root / "data")
        self.assertEqual(paths.project_key_exact(blank), roots.project_key)
        self.assertEqual(self.root / "data" / "projects" / paths.project_key_exact(blank), roots.project_data_dir)

    def test_subdir_uses_project_boundary(self):
        project = self.root / "Alpha"
        nested = project / "src" / "deep"
        (project / ".git").mkdir(parents=True)
        nested.mkdir(parents=True)

        self.assertEqual(paths.project_key(project), paths.project_key(nested))

    def test_project_roots_are_pure_paths_and_stay_independent(self):
        project = self.root / "workspace" / "Alpha"
        data = self.root / "data"
        project.mkdir(parents=True)
        data.mkdir()

        with mock.patch.dict(
            os.environ,
            {"GA_PROJECT_ROOT": str(project), "GA_DATA_DIR": str(data)},
            clear=False,
        ):
            roots = paths.project_roots(project=project, data_root=data)
            self.assertEqual(project.resolve(), roots.project_root)
            self.assertEqual(data.resolve(), roots.data_root)
            self.assertEqual(data.resolve() / "projects" / paths.project_key(project), roots.project_data_dir)
            self.assertEqual(project.resolve() / ".ga" / "subagents", roots.project_roles_dir)
            self.assertEqual(project.resolve() / ".ga" / "workflows", roots.project_workflows_dir)
            self.assertEqual(project.resolve() / ".ga" / "worktrees", roots.project_worktrees_dir)
            self.assertEqual(data.resolve() / "workflows", roots.user_workflows_dir)
            self.assertEqual(data.resolve() / "temp" / "sessions" / "named-workflows", roots.legacy_workflows_dir)

            self.assertFalse(roots.project_data_dir.exists())
            self.assertFalse(roots.project_workflows_dir.exists())

    def test_project_runtime_helpers_keep_global_and_project_scopes_separate(self):
        project = self.root / "workspace" / "Alpha"
        data = self.root / "data"
        project.mkdir(parents=True)
        roots = paths.project_roots(project=project, data_root=data)
        key = paths.team_key(roots.project_key, "session-demo")

        self.assertEqual(roots.project_data_dir, paths.project_sessions_dir(roots=roots))
        self.assertEqual(roots.project_data_dir / "workflow-runs", paths.project_workflow_runs_dir(roots=roots))
        self.assertEqual(
            roots.project_data_dir / "workflow_child_agents",
            paths.project_workflow_children_dir(roots=roots),
        )
        self.assertEqual(data / "tasks" / key, paths.task_dir(roots.project_key, "session-demo", roots=roots))
        self.assertEqual(data / "teams" / key, paths.team_dir(roots.project_key, "session-demo", roots=roots))

    def test_runtime_paths_validate_identity_consistency(self):
        project = self.root / "project"
        data = self.root / "data"
        project.mkdir()
        data.mkdir()
        roots = paths.project_roots(project=project, data_root=data)
        runtime = paths.runtime_paths(roots=roots, session_id="session_demo")

        self.assertEqual(project.resolve(), runtime.project_root)
        self.assertEqual(project.resolve(), runtime.tool_cwd)
        self.assertEqual(data.resolve(), runtime.data_root)
        self.assertEqual(roots.project_data_dir, runtime.project_data_dir)
        self.assertEqual(roots.project_data_dir / "session_demo", runtime.session_data_dir)
        self.assertEqual(roots.project_data_dir / "session_demo.jsonl", runtime.session_transcript)
        self.assertEqual(data.resolve() / "logs", runtime.logs_dir)
        self.assertEqual(data.resolve() / "media", runtime.media_dir)
        self.assertEqual(data.resolve() / "mcp", runtime.mcp_state_dir)
        with self.assertRaises(FrozenInstanceError):
            runtime.project_root = data

        with self.assertRaisesRegex(ValueError, "project_identity_mismatch"):
            paths.RuntimePaths(
                package_root=paths.PACKAGE_ROOT,
                original_cwd=project,
                project_root=project,
                launch_cwd=project,
                project_key="wrong-key",
                data_root=data,
                project_data_dir=roots.project_data_dir,
                session_data_dir=None,
                tool_cwd=project,
                worktree_root=roots.project_worktrees_dir,
                project_workflows_dir=roots.project_workflows_dir,
                user_workflows_dir=roots.user_workflows_dir,
                legacy_workflows_dir=roots.legacy_workflows_dir,
            )

    def test_two_projects_with_same_team_name_get_distinct_task_roots(self):
        from genericagent.agents.tasks import SubagentTaskStore

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project_a = root / "A"
            project_b = root / "B"
            project_a.mkdir()
            project_b.mkdir()
            data_root = root / "data"
            roots_a = paths.project_roots(project=project_a, data_root=data_root)
            roots_b = paths.project_roots(project=project_b, data_root=data_root)
            key_a = paths.team_key(roots_a.project_key, "shared-team")
            key_b = paths.team_key(roots_b.project_key, "shared-team")
            store_a = SubagentTaskStore(project_a, list_id="shared-team", tasks_root=roots_a.tasks_dir, team_key=key_a)
            store_b = SubagentTaskStore(project_b, list_id="shared-team", tasks_root=roots_b.tasks_dir, team_key=key_b)
            store_a.create_task("only A")
            self.assertEqual([], store_b.list_tasks())
            self.assertNotEqual(store_a.dir, store_b.dir)

    def test_project_buckets_are_isolated_for_sessions_and_workflows(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project_a = root / "A"
            project_b = root / "B"
            project_a.mkdir()
            project_b.mkdir()
            data_root = root / "data"
            roots_a = paths.project_roots(project=project_a, data_root=data_root)
            roots_b = paths.project_roots(project=project_b, data_root=data_root)
            self.assertNotEqual(roots_a.project_data_dir, roots_b.project_data_dir)
            self.assertEqual(roots_a.project_data_dir / "workflow-runs", paths.project_workflow_runs_dir(roots=roots_a))
            self.assertEqual(roots_b.project_data_dir / "workflow_child_agents", paths.project_workflow_children_dir(roots=roots_b))
            self.assertNotEqual(paths.project_subagents_dir("session_x", roots=roots_a), paths.project_subagents_dir("session_x", roots=roots_b))


if __name__ == "__main__":
    unittest.main()
