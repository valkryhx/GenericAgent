import tempfile
import unittest
from pathlib import Path

from genericagent.workflow.store import WorkflowStore


SCRIPT = "export const meta = { name: 'demo' }\nreturn 1"


class NamedWorkflowScopesTest(unittest.TestCase):
    def make_store(self, root):
        root = Path(root)
        return WorkflowStore(
            root=root / "runs",
            project_workflows_dir=root / "project" / ".ga" / "workflows",
            user_workflows_dir=root / "data" / "workflows",
            legacy_workflows_dir=root / "data" / "temp" / "sessions" / "named-workflows",
        ), root

    def test_project_definition_shadows_user_and_legacy(self):
        with tempfile.TemporaryDirectory() as td:
            store, root = self.make_store(td)
            store.save_named_workflow("demo", "legacy", scope="legacy")
            store.save_named_workflow("demo", "user", scope="user")
            store.save_named_workflow("demo", "project", scope="project")

            loaded = store.load_named_workflow("demo")
            self.assertEqual("project", loaded["script"])
            self.assertEqual("project", loaded["scope"])
            listed = store.list_named_workflows(include_shadowed=True)
            self.assertEqual(3, len(listed))
            self.assertEqual("project", listed[0]["scope"])

    def test_auto_loads_user_when_project_is_absent(self):
        with tempfile.TemporaryDirectory() as td:
            store, _root = self.make_store(td)
            store.save_named_workflow("demo", SCRIPT, scope="user")

            loaded = store.load_named_workflow("demo")
            self.assertEqual("user", loaded["scope"])
            self.assertEqual(SCRIPT, loaded["script"])

    def test_project_save_does_not_fallback_to_user(self):
        with tempfile.TemporaryDirectory() as td:
            store, root = self.make_store(td)
            saved = store.save_named_workflow("demo", SCRIPT, scope="project")

            self.assertEqual("project", saved["scope"])
            self.assertTrue((root / "project" / ".ga" / "workflows" / "demo.json").is_file())
            self.assertFalse((root / "data" / "workflows" / "demo.json").exists())

    def test_missing_auto_error_names_candidate_scopes(self):
        with tempfile.TemporaryDirectory() as td:
            store, _root = self.make_store(td)
            with self.assertRaisesRegex(FileNotFoundError, "candidates:"):
                store.load_named_workflow("missing")


    def test_default_save_scope_is_project_per_plan(self):
        with tempfile.TemporaryDirectory() as td:
            store, root = self.make_store(td)
            saved = store.save_named_workflow("demo", SCRIPT)
            self.assertEqual("project", saved["scope"])
            self.assertTrue((root / "project" / ".ga" / "workflows" / "demo.json").is_file())

    def test_corrupt_project_definition_does_not_fall_back_to_user(self):
        with tempfile.TemporaryDirectory() as td:
            store, root = self.make_store(td)
            store.save_named_workflow("demo", SCRIPT, scope="user")
            project = root / "project" / ".ga" / "workflows"
            project.mkdir(parents=True)
            (project / "demo.json").write_text("{broken", encoding="utf-8")
            with self.assertRaises(Exception):
                store.load_named_workflow("demo")

    def test_same_scope_collision_is_explicit(self):
        with tempfile.TemporaryDirectory() as td:
            store, _root = self.make_store(td)
            store.save_named_workflow("demo", SCRIPT, scope="project")
            with self.assertRaises(FileExistsError):
                store.save_named_workflow("demo", "return 2", scope="project")


if __name__ == "__main__":
    unittest.main()
