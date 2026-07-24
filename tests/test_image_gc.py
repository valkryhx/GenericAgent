"""image_gc 单元测试：只删自产 clipboard/upload，过期与条数上限。"""

from __future__ import annotations

import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import image_gc as ig  # noqa: E402


class ImageGcTest(unittest.TestCase):
    def test_is_owned_image_file(self):
        self.assertTrue(ig.is_owned_image_file(Path("clipboard-1-abc.png")))
        self.assertTrue(ig.is_owned_image_file(Path("upload-x.jpg")))
        self.assertFalse(ig.is_owned_image_file(Path("图2.png")))
        self.assertFalse(ig.is_owned_image_file(Path("notes.txt")))
        self.assertFalse(ig.is_owned_image_file(Path("session.jsonl")))

    def test_gc_deletes_old_owned_keeps_new_and_user_files(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            old = root / "clipboard-old.png"
            new = root / "clipboard-new.png"
            user = root / "user-shot.png"
            sub = root / "sess1"
            sub.mkdir()
            old_sub = sub / "clipboard-old-sub.png"
            for p in (old, new, user, old_sub):
                p.write_bytes(b"\x89PNG\r\n\x1a\n" + b"x" * 50)

            now = time.time()
            old_mtime = now - 10 * 86400
            new_mtime = now - 1 * 86400
            os.utime(old, (old_mtime, old_mtime))
            os.utime(old_sub, (old_mtime, old_mtime))
            os.utime(new, (new_mtime, new_mtime))
            os.utime(user, (old_mtime, old_mtime))  # 即使旧也不删：非 owned 前缀

            result = ig.gc_ga_images_root(root, retain_days=7, max_per_dir=200, now=now)

            self.assertFalse(old.exists())
            self.assertFalse(old_sub.exists())
            self.assertTrue(new.exists())
            self.assertTrue(user.exists(), "user file must never be deleted")
            self.assertGreaterEqual(result.deleted_count, 2)

    def test_gc_enforces_max_files_per_dir(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            now = time.time()
            paths = []
            for i in range(5):
                p = root / f"clipboard-{i}.png"
                p.write_bytes(b"png" + bytes([i]))
                # 更旧的 mtime 更小
                m = now - (10 - i) * 60
                os.utime(p, (m, m))
                paths.append(p)

            result = ig.gc_ga_images_root(root, retain_days=365, max_per_dir=3, now=now)
            remaining = sorted(root.glob("clipboard-*.png"), key=lambda x: x.name)
            self.assertEqual(len(remaining), 3)
            # 应保留最新的 2,3,4
            names = {p.name for p in remaining}
            self.assertEqual(names, {"clipboard-2.png", "clipboard-3.png", "clipboard-4.png"})
            self.assertEqual(result.deleted_count, 2)

    def test_gc_does_not_touch_missing_root(self):
        result = ig.gc_ga_images_root(Path(tempfile.gettempdir()) / "ga-images-no-such-xyz")
        self.assertEqual(result.deleted_count, 0)

    def test_resolve_ga_image_root_env(self):
        with tempfile.TemporaryDirectory() as d:
            old = os.environ.get("GA_IMAGE_TEMP")
            try:
                os.environ["GA_IMAGE_TEMP"] = d
                root = ig.resolve_ga_image_root()
                self.assertEqual(root, Path(d).resolve())
            finally:
                if old is None:
                    os.environ.pop("GA_IMAGE_TEMP", None)
                else:
                    os.environ["GA_IMAGE_TEMP"] = old


if __name__ == "__main__":
    unittest.main()
