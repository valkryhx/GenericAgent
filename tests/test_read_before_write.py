"""写前必读门禁测试。"""

import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from genericagent.core.read_before_write import ReadBeforeWriteRegistry  # noqa: E402


class ReadBeforeWriteTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.path = self.root / "target.txt"
        self.path.write_text("original\n", encoding="utf-8")
        self.registry = ReadBeforeWriteRegistry()

    def tearDown(self):
        self._tmp.cleanup()

    def test_unread_file_is_rejected(self):
        verdict = self.registry.check_write(str(self.path))
        self.assertFalse(verdict.allowed)
        self.assertIn("has not been read", verdict.reason)
        # The message must name the fix, not just the failure.
        self.assertIn("file_read", verdict.reason)

    def test_read_then_write_is_allowed(self):
        self.registry.record_read(str(self.path), complete=True)
        self.assertTrue(self.registry.check_write(str(self.path)).allowed)

    def test_partial_read_is_rejected(self):
        # Reading 200 lines of a 10,000-line file does not license an overwrite.
        self.registry.record_read(str(self.path), complete=False)
        verdict = self.registry.check_write(str(self.path))
        self.assertFalse(verdict.allowed)
        self.assertIn("Only part", verdict.reason)

    def test_external_modification_after_read_is_rejected(self):
        self.registry.record_read(str(self.path), complete=True)
        self.path.write_text("changed by someone else\n", encoding="utf-8")
        verdict = self.registry.check_write(str(self.path))
        self.assertFalse(verdict.allowed)
        self.assertIn("changed on disk", verdict.reason)

    def test_touch_without_content_change_is_allowed(self):
        # mtime moved but the bytes did not: `git checkout`, a no-op formatter or
        # a plain touch. Blocking these would train users to ignore the gate.
        self.registry.record_read(str(self.path), complete=True)
        stat = self.path.stat()
        os.utime(str(self.path), ns=(stat.st_atime_ns, stat.st_mtime_ns + 10_000_000))
        self.assertTrue(self.registry.check_write(str(self.path)).allowed)

    def test_new_file_is_allowed_without_reading(self):
        fresh = self.root / "brand-new.txt"
        self.assertTrue(self.registry.check_write(str(fresh)).allowed)

    def test_append_and_prepend_do_not_require_a_read(self):
        for mode in ("append", "prepend"):
            with self.subTest(mode=mode):
                self.assertTrue(self.registry.check_write(str(self.path), mode=mode).allowed)

    def test_overwrite_mode_still_requires_a_read(self):
        # Guard against a regression where mode handling accidentally exempts all.
        self.assertFalse(self.registry.check_write(str(self.path), mode="overwrite").allowed)

    def test_forget_clears_the_record(self):
        self.registry.record_read(str(self.path), complete=True)
        self.assertTrue(self.registry.check_write(str(self.path)).allowed)
        self.registry.forget(str(self.path))
        self.assertFalse(self.registry.check_write(str(self.path)).allowed)

    def test_registries_are_independent(self):
        # Two agents must not vouch for each other.
        other = ReadBeforeWriteRegistry()
        self.registry.record_read(str(self.path), complete=True)
        self.assertFalse(other.check_write(str(self.path)).allowed)

    def test_write_allowed_again_after_reread(self):
        self.registry.record_read(str(self.path), complete=True)
        self.path.write_text("changed\n", encoding="utf-8")
        self.assertFalse(self.registry.check_write(str(self.path)).allowed)
        # Re-reading refreshes the snapshot.
        self.registry.record_read(str(self.path), complete=True)
        self.assertTrue(self.registry.check_write(str(self.path)).allowed)


if __name__ == "__main__":
    unittest.main()
