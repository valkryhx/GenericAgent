import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from genericagent import paths
from genericagent.llm.config import find_llm_config


class LlmConfigPathResolutionTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_explicit_start_dir_keeps_historical_behavior(self):
        cfg = self.root / "llm.yaml"
        cfg.write_text("providers: {}\n", encoding="utf-8")
        self.assertEqual(str(cfg), find_llm_config(str(self.root)))

    def test_explicit_start_dir_does_not_fall_back_elsewhere(self):
        with patch.dict(os.environ, {"GA_LLM_CONFIG": ""}, clear=False):
            self.assertIsNone(find_llm_config(str(self.root)))

    def test_ga_llm_config_has_highest_priority(self):
        override = self.root / "custom.yaml"
        override.write_text("providers: {}\n", encoding="utf-8")
        with patch.dict(os.environ, {"GA_LLM_CONFIG": str(override)}, clear=False):
            self.assertEqual(override.resolve(), paths.resolve_llm_config_path())

    def test_user_config_dir_uses_ga_config_dir(self):
        with patch.dict(os.environ, {"GA_CONFIG_DIR": str(self.root)}, clear=False):
            self.assertEqual(self.root.resolve(), paths.user_config_dir())

    def test_seed_copies_example_and_never_overwrites(self):
        template = self.root / "llm.yaml.example"
        template.write_text("template" + chr(10), encoding="utf-8")
        config_dir = self.root / "user-config"

        with patch.object(paths, "PROJECT_ROOT", self.root):
            with patch.object(paths, "BUNDLED_ASSETS_DIR", self.root / "missing-assets"):
                with patch.dict(os.environ, {"GA_CONFIG_DIR": str(config_dir)}, clear=False):
                    created = paths.seed_llm_config()
                    self.assertEqual(config_dir / "llm.yaml", created)
                    self.assertEqual("template" + chr(10), created.read_text(encoding="utf-8"))

                    created.write_text("user-edited" + chr(10), encoding="utf-8")
                    self.assertIsNone(paths.seed_llm_config())
                    self.assertEqual("user-edited" + chr(10), created.read_text(encoding="utf-8"))

    def test_runtime_assets_are_seeded_without_overwriting_user_files(self):
        bundled_assets = self.root / "bundled-assets"
        bundled_skills = self.root / "bundled-skills"
        runtime = self.root / "runtime"
        bundled_assets.mkdir()
        (bundled_assets / "prompt.txt").write_text("bundled\n", encoding="utf-8")
        (bundled_skills / "sample").mkdir(parents=True)
        (bundled_skills / "sample" / "SKILL.md").write_text("skill\n", encoding="utf-8")
        existing = runtime / "assets" / "prompt.txt"
        existing.parent.mkdir(parents=True)
        existing.write_text("user\n", encoding="utf-8")

        with patch.object(paths, "PROJECT_ROOT", runtime):
            with patch.object(paths, "TEMP_DIR", runtime / "temp"):
                with patch.object(paths, "SESSIONS_DIR", runtime / "temp" / "sessions"):
                    with patch.object(paths, "ASSETS_DIR", runtime / "assets"):
                        with patch.object(paths, "SKILLS_DIR", runtime / "skills"):
                            with patch.object(paths, "BUNDLED_ASSETS_DIR", bundled_assets):
                                with patch.object(paths, "BUNDLED_SKILLS_DIR", bundled_skills):
                                    paths.ensure_runtime_dirs()

        self.assertEqual("user\n", existing.read_text(encoding="utf-8"))
        self.assertEqual("skill\n", (runtime / "skills" / "sample" / "SKILL.md").read_text(encoding="utf-8"))

    def test_installed_llm_template_falls_back_to_package_resources(self):
        resources = self.root / "genericagent" / "resources"
        resources.mkdir(parents=True)
        template = resources / "llm.yaml.example"
        template.write_text("packaged\n", encoding="utf-8")

        with patch.object(paths, "PROJECT_ROOT", self.root / "user-data"):
            with patch.object(paths, "BUNDLED_ASSETS_DIR", resources / "assets"):
                self.assertEqual(template, paths.llm_template_path())


class InstalledLayoutPathTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.addCleanup(self.tmp.cleanup)

    def test_installed_layout_prefers_user_config_and_user_data(self):
        package = self.root / "site-packages" / "genericagent"
        package.mkdir(parents=True)
        workspace = self.root / "user-project"
        workspace.mkdir()
        # A user project may itself be a Python project. Its pyproject.toml must
        # not make the installed GA package believe it is a source checkout.
        (workspace / "pyproject.toml").write_text("[project]" + chr(10), encoding="utf-8")
        data = self.root / "data"
        config = self.root / "config"
        with patch.object(paths, "PACKAGE_ROOT", package):
            with patch.object(paths, "PROJECT_ROOT", workspace):
                with patch.dict(os.environ, {"GA_DATA_DIR": str(data), "GA_CONFIG_DIR": str(config)}, clear=False):
                    candidates = paths.llm_config_candidates()
                    self.assertEqual(data.resolve(), paths.runtime_root())
                    self.assertEqual([config / "llm.yaml", config / "llm.yml"], candidates)

    def test_source_checkout_keeps_project_local_config_priority(self):
        project = self.root / "project"
        project.mkdir()
        (project / "pyproject.toml").write_text("[project]" + chr(10), encoding="utf-8")
        config = self.root / "config"
        with patch.object(paths, "PACKAGE_ROOT", project / "genericagent"):
            (project / "genericagent").mkdir()
            with patch.object(paths, "PROJECT_ROOT", project):
                with patch.dict(os.environ, {"GA_CONFIG_DIR": str(config)}, clear=False):
                    candidates = paths.llm_config_candidates()
        self.assertEqual(project / "llm.yaml", candidates[0])



if __name__ == "__main__":
    unittest.main()
