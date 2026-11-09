"""Bundled runtime assets must refresh, and must not drift from the repo copy.

Why this exists (2026-11-10): `file_glob` / `file_grep` were added to
``assets/tools_schema*.json`` but never to ``genericagent/resources/assets/``,
and ``ensure_runtime_assets`` only copied *missing* files. An installed GA
therefore never saw the new tools at all, and a runtime directory seeded once
kept an old schema (31 tools) for ever -- which reads exactly like "the model
refuses to use the tools I configured".

The contract:
* a file we shipped and that the user has not edited is refreshed;
* a file the user edited is preserved, and stays preserved on later upgrades;
* a file we never recorded (legacy install, hand-placed file) is adopted as-is,
  never overwritten;
* ``GA_REFRESH_ASSETS=1`` forces the overwrite for a deliberate repair.
"""

import hashlib
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from genericagent import paths


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class RuntimeAssetRefreshTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.bundled = self.root / "bundled-assets"
        self.runtime = self.root / "runtime"
        self.bundled.mkdir()
        (self.runtime / "assets").mkdir(parents=True)

    def _seed(self, *, force=False):
        env = {"GA_REFRESH_ASSETS": "1"} if force else {}
        with mock.patch.object(paths, "ASSETS_DIR", self.runtime / "assets"), \
             mock.patch.object(paths, "SKILLS_DIR", self.runtime / "skills"), \
             mock.patch.object(paths, "BUNDLED_ASSETS_DIR", self.bundled), \
             mock.patch.object(paths, "BUNDLED_SKILLS_DIR", self.root / "bundled-skills"), \
             mock.patch.dict(os.environ, env, clear=False):
            if not force:
                os.environ.pop("GA_REFRESH_ASSETS", None)
            paths.ensure_runtime_assets()

    def _write_manifest(self, mapping):
        import json
        (self.runtime / "assets" / paths.ASSET_MANIFEST_NAME).write_text(
            json.dumps({"version": 1, "files": mapping}), encoding="utf-8")

    def test_a_managed_asset_is_refreshed_when_the_bundle_changes(self):
        (self.bundled / "tools_schema.json").write_text("v2", encoding="utf-8")
        (self.runtime / "assets" / "tools_schema.json").write_text("v1", encoding="utf-8")
        self._write_manifest({"tools_schema.json": _sha("v1")})

        self._seed()

        self.assertEqual("v2", (self.runtime / "assets" / "tools_schema.json").read_text(encoding="utf-8"))

    def test_a_user_edited_asset_is_preserved(self):
        (self.bundled / "tools_schema.json").write_text("v2", encoding="utf-8")
        (self.runtime / "assets" / "tools_schema.json").write_text("mine", encoding="utf-8")
        self._write_manifest({"tools_schema.json": _sha("v1")})

        self._seed()

        self.assertEqual("mine", (self.runtime / "assets" / "tools_schema.json").read_text(encoding="utf-8"))

    def test_an_unrecorded_asset_is_adopted_but_never_overwritten(self):
        # A legacy install has no manifest. Its file may be an old bundled copy
        # or a user edit -- unknowable, so preserve it and adopt it as baseline.
        (self.bundled / "tools_schema.json").write_text("v2", encoding="utf-8")
        (self.runtime / "assets" / "tools_schema.json").write_text("old", encoding="utf-8")

        self._seed()
        self.assertEqual("old", (self.runtime / "assets" / "tools_schema.json").read_text(encoding="utf-8"))

        # Having adopted it, later bundles still must not clobber it.
        (self.bundled / "tools_schema.json").write_text("v3", encoding="utf-8")
        self._seed()
        self.assertEqual("old", (self.runtime / "assets" / "tools_schema.json").read_text(encoding="utf-8"))

    def test_missing_assets_are_copied_and_recorded(self):
        (self.bundled / "sys_prompt.txt").write_text("base", encoding="utf-8")

        self._seed()

        copied = self.runtime / "assets" / "sys_prompt.txt"
        self.assertEqual("base", copied.read_text(encoding="utf-8"))
        import json
        manifest = json.loads((self.runtime / "assets" / paths.ASSET_MANIFEST_NAME).read_text(encoding="utf-8"))
        self.assertEqual(_sha("base"), manifest["files"]["sys_prompt.txt"])

    def test_force_refresh_overwrites_a_legacy_copy(self):
        (self.bundled / "tools_schema.json").write_text("v2", encoding="utf-8")
        (self.runtime / "assets" / "tools_schema.json").write_text("old", encoding="utf-8")

        self._seed(force=True)

        self.assertEqual("v2", (self.runtime / "assets" / "tools_schema.json").read_text(encoding="utf-8"))

    def test_skills_are_synced_with_the_same_contract(self):
        skills = self.root / "bundled-skills" / "sample"
        skills.mkdir(parents=True)
        (skills / "SKILL.md").write_text("skill", encoding="utf-8")

        self._seed()

        self.assertEqual("skill", (self.runtime / "skills" / "sample" / "SKILL.md").read_text(encoding="utf-8"))


class BundledAssetsParityTest(unittest.TestCase):
    """The packaged copy is what an installed GA reads; it may not lag the repo."""

    FILES = ("tools_schema.json", "tools_schema_cn.json", "sys_prompt.txt", "sys_prompt_en.txt")

    def setUp(self):
        self.repo = Path(__file__).resolve().parent.parent
        self.runtime_assets = self.repo / "assets"
        self.bundled = self.repo / "genericagent" / "resources" / "assets"

    def test_bundled_assets_match_the_repo_copy(self):
        for name in self.FILES:
            with self.subTest(name=name):
                expected = (self.runtime_assets / name).read_bytes()
                actual = (self.bundled / name).read_bytes()
                self.assertEqual(expected, actual,
                                 f"genericagent/resources/assets/{name} drifted from assets/{name}")

    def test_the_schema_exposes_the_cc_style_search_tools(self):
        import json
        for name in ("tools_schema.json", "tools_schema_cn.json"):
            with self.subTest(name=name):
                for root in (self.runtime_assets, self.bundled):
                    tools = json.loads((root / name).read_text(encoding="utf-8"))
                    names = {item["function"]["name"] for item in tools}
                    self.assertTrue({"file_glob", "file_grep"}.issubset(names),
                                    f"{root / name} lacks file_glob/file_grep")


if __name__ == "__main__":
    unittest.main()
