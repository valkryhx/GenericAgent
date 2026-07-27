import tempfile
import unittest
from pathlib import Path


from ga_agents_runtime import (
    DEFAULT_GA_AGENTS_FILENAME,
    LOCAL_GA_AGENTS_FILENAME,
    build_ga_project_instructions,
    discover_ga_agents_paths,
    load_ga_project_instructions,
)


class GaAgentsRuntimeTest(unittest.TestCase):
    def test_discovers_docs_from_workspace_root_to_current_dir(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            child = root / "frontends" / "ink-ui"
            child.mkdir(parents=True)
            (root / DEFAULT_GA_AGENTS_FILENAME).write_text("root rules", encoding="utf-8")
            (root / "frontends" / DEFAULT_GA_AGENTS_FILENAME).write_text("frontend rules", encoding="utf-8")
            (child / DEFAULT_GA_AGENTS_FILENAME).write_text("ink rules", encoding="utf-8")

            paths = discover_ga_agents_paths(root, child)

            self.assertEqual(
                [
                    root / DEFAULT_GA_AGENTS_FILENAME,
                    root / "frontends" / DEFAULT_GA_AGENTS_FILENAME,
                    child / DEFAULT_GA_AGENTS_FILENAME,
                ],
                paths,
            )

    def test_override_replaces_default_file_only_in_same_directory(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            child = root / "pkg"
            child.mkdir()
            (root / DEFAULT_GA_AGENTS_FILENAME).write_text("root default", encoding="utf-8")
            (root / LOCAL_GA_AGENTS_FILENAME).write_text("root override", encoding="utf-8")
            (child / DEFAULT_GA_AGENTS_FILENAME).write_text("child default", encoding="utf-8")
            (child / LOCAL_GA_AGENTS_FILENAME).write_text("child override", encoding="utf-8")

            loaded = load_ga_project_instructions(root, child)

            self.assertEqual(
                [
                    str(Path(LOCAL_GA_AGENTS_FILENAME)),
                    str(Path("pkg") / LOCAL_GA_AGENTS_FILENAME),
                ],
                [doc.rel_path for doc in loaded.docs],
            )
            rendered = build_ga_project_instructions(root, child)
            self.assertIn("root override", rendered)
            self.assertIn("child override", rendered)
            self.assertNotIn("root default", rendered)
            self.assertNotIn("child default", rendered)

    def test_zero_budget_disables_project_instructions(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / DEFAULT_GA_AGENTS_FILENAME).write_text("root rules", encoding="utf-8")

            loaded = load_ga_project_instructions(root, root, max_bytes=0)

            self.assertEqual((), loaded.docs)
            self.assertEqual("", build_ga_project_instructions(root, root, max_bytes=0))

    def test_budget_truncates_total_loaded_content(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            child = root / "pkg"
            child.mkdir()
            (root / DEFAULT_GA_AGENTS_FILENAME).write_text("abcdef", encoding="utf-8")
            (child / DEFAULT_GA_AGENTS_FILENAME).write_text("child rules", encoding="utf-8")

            loaded = load_ga_project_instructions(root, child, max_bytes=4)

            self.assertEqual(1, len(loaded.docs))
            self.assertEqual("abcd", loaded.docs[0].content)
            self.assertTrue(loaded.docs[0].truncated)
            self.assertTrue(loaded.truncated)

    def test_rendered_block_keeps_sources_and_root_to_cwd_order(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            child = root / "pkg"
            child.mkdir()
            (root / DEFAULT_GA_AGENTS_FILENAME).write_text("root rules", encoding="utf-8")
            (child / DEFAULT_GA_AGENTS_FILENAME).write_text("child rules", encoding="utf-8")

            rendered = build_ga_project_instructions(root, child)

            self.assertIn("[GA_PROJECT_INSTRUCTIONS]", rendered)
            self.assertIn(f"Source: {DEFAULT_GA_AGENTS_FILENAME}", rendered)
            self.assertIn(f"Source: {Path('pkg') / DEFAULT_GA_AGENTS_FILENAME}", rendered)
            self.assertLess(rendered.index("root rules"), rendered.index("child rules"))
            self.assertIn("later and more specific source", rendered)

    def test_current_dir_outside_workspace_falls_back_to_workspace_root(self):
        with tempfile.TemporaryDirectory() as td, tempfile.TemporaryDirectory() as other:
            root = Path(td)
            outside = Path(other)
            (root / DEFAULT_GA_AGENTS_FILENAME).write_text("root rules", encoding="utf-8")
            (outside / DEFAULT_GA_AGENTS_FILENAME).write_text("outside rules", encoding="utf-8")

            rendered = build_ga_project_instructions(root, outside)

            self.assertIn("root rules", rendered)
            self.assertNotIn("outside rules", rendered)


if __name__ == "__main__":
    unittest.main()
