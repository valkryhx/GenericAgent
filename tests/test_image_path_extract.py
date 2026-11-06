"""O3 后端路径扫描收紧：跳过代码块/URL/key=value，上限 8，可关闭。"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import genericagent.core.app as app  # noqa: E402


class ExtractImagePathsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.imgs = []
        for name in ("a.png", "b.jpg", "logo.png"):
            p = self.dir / name
            p.write_bytes(b"\x89PNG\r\n\x1a\n" + name.encode())
            self.imgs.append(p)

    def tearDown(self):
        self._tmp.cleanup()

    def test_plain_absolute_path_is_extracted(self):
        text = f"看看这张图 {self.imgs[0]}"
        got = app._extract_image_paths(text)
        self.assertEqual(got, [str(self.imgs[0])])

    def test_code_fence_path_skipped(self):
        text = f"```\nassets/{self.imgs[0].name}\n```\n这段是配置代码"
        # 代码块内路径不应被当作附件（即使文件真实存在）
        self.assertEqual(app._extract_image_paths(text), [])

    def test_inline_code_path_skipped(self):
        text = f"配置项 `{self.imgs[1]}` 只是示例"
        self.assertEqual(app._extract_image_paths(text), [])

    def test_url_path_skipped(self):
        text = "参考 https://example.com/x/logo.png 这张远程图"
        self.assertEqual(app._extract_image_paths(text), [])

    def test_key_value_path_skipped(self):
        # 第 3 条裸 token 规则跳过 key=value 形态（文件相对 cwd 不存在也不该命中）
        old = os.getcwd()
        os.chdir(self.dir)
        try:
            text = f"ICON={self.imgs[2].name}"
            self.assertEqual(app._extract_image_paths(text), [])
        finally:
            os.chdir(old)

    def test_limit_eight_and_dropped_count(self):
        ps = []
        for i in range(12):
            p = self.dir / f"n{i}.png"
            p.write_bytes(b"\x89PNG\r\n\x1a\n")
            ps.append(str(p))
        text = " ".join(ps)
        with mock.patch.object(app, "MAX_AUTO_IMAGE_PATHS", 8):
            paths, dropped = app._extract_image_paths_report(text)
        self.assertEqual(len(paths), 8)
        self.assertEqual(dropped, 4)

    def test_auto_paths_disabled(self):
        text = f"看看 {self.imgs[0]}"
        with mock.patch.dict(os.environ, {"GA_IMAGE_AUTO_PATHS": "0"}):
            self.assertEqual(app._extract_image_paths(text), [])

    def test_quoted_path_with_spaces(self):
        p = self.dir / "with space.png"
        p.write_bytes(b"\x89PNG\r\n\x1a\n")
        text = f'看 "{p}" 这张'
        self.assertEqual(app._extract_image_paths(text), [str(p)])



class TruncationNoticeTest(unittest.TestCase):
    def test_truncation_notice_appears_in_content(self):
        import genericagent.core.app as app
        try:
            from PIL import Image
        except Exception:
            self.skipTest("Pillow not installed")
        import tempfile, os
        with tempfile.TemporaryDirectory() as d:
            ps = []
            for i in range(12):
                p = os.path.join(d, f"m{i}.png")
                Image.new("RGB", (16, 16), (i * 20 % 255, 40, 80)).save(p, format="PNG")
                ps.append(p)
            content = app._build_user_content_with_images("看这些图 " + " ".join(ps))
        self.assertIsNotNone(content)
        text = content[0]["text"]
        self.assertIn("其余 4 个路径未自动附图", text)
        self.assertIn("上限 8", text)
        # 只发 8 张 image block（content[0] 是 text）
        self.assertEqual(len([b for b in content if b.get("type") == "image"]), 8)



if __name__ == "__main__":
    unittest.main()
