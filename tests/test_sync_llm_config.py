"""Tests for scripts/sync_llm_config.py.

The script exists because a source checkout and an installed wheel read
DIFFERENT llm.yaml files:

  * source checkout -> <repo>/llm.yaml
  * installed wheel -> %APPDATA%/GenericAgent/llm.yaml

``llm_no`` is an index into whichever file the process resolved, so the two
copies must stay aligned.  One extra profile in one of them shifts every later
index and a subagent silently starts using a different model than its parent
asked for.  These tests pin the copy semantics that keep them aligned.
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_sync_module():
    """Import scripts/sync_llm_config.py, which is not an importable package."""
    path = REPO_ROOT / "scripts" / "sync_llm_config.py"
    spec = importlib.util.spec_from_file_location("ga_sync_llm_config", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


sync_mod = _load_sync_module()


class SyncLlmConfigTest(unittest.TestCase):
    def _write(self, path: Path, text: str) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
        return path

    def test_copies_source_over_target_when_they_differ(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = self._write(root / "repo" / "llm.yaml", "providers:\n  a: 1\n")
            target = self._write(root / "cfg" / "llm.yaml", "providers:\n  b: 2\n")

            result = sync_mod.sync_config(source, target)

            self.assertTrue(result["changed"])
            self.assertEqual(target.read_text(encoding="utf-8"), source.read_text(encoding="utf-8"))

    def test_identical_content_is_not_rewritten(self):
        """A no-op sync must not touch the file: rewriting the same bytes would
        bump mtime, and agentmain hot-reloads llm.yaml on mtime change."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = self._write(root / "repo" / "llm.yaml", "providers:\n  a: 1\n")
            target = self._write(root / "cfg" / "llm.yaml", "providers:\n  a: 1\n")
            before = target.stat().st_mtime_ns

            result = sync_mod.sync_config(source, target)

            self.assertFalse(result["changed"])
            self.assertEqual(target.stat().st_mtime_ns, before)

    def test_existing_target_is_backed_up_before_overwrite(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = self._write(root / "repo" / "llm.yaml", "providers:\n  new: 1\n")
            target = self._write(root / "cfg" / "llm.yaml", "providers:\n  old: 1\n")

            result = sync_mod.sync_config(source, target)

            backup = Path(result["backup"])
            self.assertTrue(backup.is_file())
            self.assertIn("old", backup.read_text(encoding="utf-8"))

    def test_dry_run_reports_change_without_writing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = self._write(root / "repo" / "llm.yaml", "providers:\n  new: 1\n")
            target = self._write(root / "cfg" / "llm.yaml", "providers:\n  old: 1\n")

            result = sync_mod.sync_config(source, target, dry_run=True)

            self.assertTrue(result["changed"])
            self.assertIn("old", target.read_text(encoding="utf-8"))
            self.assertEqual(result["backup"], "")

    def test_missing_source_is_an_error_not_an_empty_overwrite(self):
        """Never let a mistyped path blank the user's working config."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            target = self._write(root / "cfg" / "llm.yaml", "providers:\n  keep: 1\n")

            with self.assertRaises(FileNotFoundError):
                sync_mod.sync_config(root / "repo" / "missing.yaml", target)

            self.assertIn("keep", target.read_text(encoding="utf-8"))

    def test_target_directory_is_created_for_first_run(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source = self._write(root / "repo" / "llm.yaml", "providers:\n  a: 1\n")
            target = root / "fresh" / "nested" / "llm.yaml"

            result = sync_mod.sync_config(source, target)

            self.assertTrue(result["changed"])
            self.assertTrue(target.is_file())
            self.assertEqual(result["backup"], "")

    def test_profile_index_stays_aligned_after_sync(self):
        """The property that actually matters: profile order must match, so an
        llm_no index resolves to the same profile in both files."""
        import yaml

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            source_text = (
                "providers:\n  p:\n    wire_api: openai_chat\n    base_url: http://x\n    api_key: k\n"
                "models:\n  m1:\n    provider: p\n    api_model: m1\n"
                "  m2:\n    provider: p\n    api_model: m2\n"
                "profiles:\n  first:\n    model: m1\n  second:\n    model: m2\n"
                "defaults:\n  max_tokens: 8\n"
                "active_profile: second\n"
            )
            stale = "providers:\n  p:\n    wire_api: openai_chat\n    base_url: http://x\n    api_key: k\nmodels: {}\nprofiles:\n  only:\n    model: m1\n"
            source = self._write(root / "repo" / "llm.yaml", source_text)
            target = self._write(root / "cfg" / "llm.yaml", stale)

            sync_mod.sync_config(source, target)

            src_profiles = list(yaml.safe_load(source.read_text(encoding="utf-8"))["profiles"])
            dst_profiles = list(yaml.safe_load(target.read_text(encoding="utf-8"))["profiles"])
            self.assertEqual(src_profiles, dst_profiles)


if __name__ == "__main__":
    unittest.main()