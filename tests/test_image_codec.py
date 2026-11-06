"""image_codec 单元测试：有界编码、体积上限、缓存、Claude image block。"""

from __future__ import annotations

import base64
import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import genericagent.media.image_codec as ic  # noqa: E402


# 8x8 PNG（满足 MIN_DIMENSION=8；1x1 会被 Grok 等 provider 拒绝）
_TINY_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAgAAAAICAIAAABLbSncAAAAFElEQVR4nGM8YWTEgA0wYRUdtBIA76YBPGvKGPUAAAAASUVORK5CYII="
)
# 1x1 PNG — 用于断言「过小图」被拒
_ONE_PX_PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
)


class EncodeImageForPromptTest(unittest.TestCase):
    def setUp(self):
        ic.clear_encode_cache()

    def test_encodes_small_png_to_base64(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(_TINY_PNG)
            path = Path(f.name)
        try:
            enc = ic.encode_image_for_prompt(str(path))
        finally:
            path.unlink(missing_ok=True)

        self.assertEqual(enc.media_type, "image/png")
        self.assertEqual(base64.b64decode(enc.data_b64), _TINY_PNG)
        self.assertEqual(enc.sha1, hashlib.sha1(_TINY_PNG).hexdigest())
        self.assertFalse(enc.resized)
        self.assertGreaterEqual(enc.width, ic.MIN_DIMENSION)
        self.assertGreaterEqual(enc.height, ic.MIN_DIMENSION)

    def test_rejects_sub_min_dimension_png(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(_ONE_PX_PNG)
            path = Path(f.name)
        try:
            with self.assertRaises(ic.ImageCodecError) as ctx:
                ic.encode_image_for_prompt(str(path))
            self.assertIn("too small", str(ctx.exception).lower())
        finally:
            path.unlink(missing_ok=True)

    def test_missing_file_raises(self):
        with self.assertRaises(ic.ImageCodecError):
            ic.encode_image_for_prompt(str(REPO_ROOT / "no_such_image_xyz.png"))

    def test_rejects_oversize_raw_file(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            # 不真写 40MB：用 monkeypatch 阈值
            f.write(_TINY_PNG)
            path = Path(f.name)
        try:
            old = ic.MAX_RAW_FILE_BYTES
            ic.MAX_RAW_FILE_BYTES = 10
            try:
                with self.assertRaises(ic.ImageCodecError):
                    ic.encode_image_for_prompt(str(path))
            finally:
                ic.MAX_RAW_FILE_BYTES = old
        finally:
            path.unlink(missing_ok=True)

    def test_rejects_oversize_base64_without_pillow_resize(self):
        # 构造「解码后 base64 仍超限」的假路径：用超长伪文件 + 跳过真实缩放
        # 当无法缩小到 MAX_BASE64_CHARS 时必须抛错，禁止静默塞巨图。
        blob = b"\x89PNG\r\n\x1a\n" + (b"A" * (ic.MAX_BASE64_CHARS + 100))
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f:
            f.write(blob)
            path = Path(f.name)
        try:
            # 魔数不是合法 PNG 解码时，仍应按 base64 长度拒绝
            with self.assertRaises(ic.ImageCodecError):
                ic.encode_image_for_prompt(str(path))
        finally:
            path.unlink(missing_ok=True)

    def test_cache_hits_same_sha1(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(_TINY_PNG)
            path = Path(f.name)
        try:
            a = ic.encode_image_for_prompt(str(path))
            b = ic.encode_image_for_prompt(str(path))
            self.assertIs(a, b)
        finally:
            path.unlink(missing_ok=True)

    def test_build_claude_image_block(self):
        enc = ic.EncodedImage(
            data_b64="AAAA",
            media_type="image/png",
            width=1,
            height=1,
            source_path="/tmp/x.png",
            sha1="deadbeef",
            resized=False,
        )
        block = ic.build_image_block(enc)
        self.assertEqual(block["type"], "image")
        self.assertEqual(block["source"]["type"], "base64")
        self.assertEqual(block["source"]["media_type"], "image/png")
        self.assertEqual(block["source"]["data"], "AAAA")


class NormalizeAndBuildContentTest(unittest.TestCase):
    def setUp(self):
        ic.clear_encode_cache()

    def test_normalize_accepts_path_strings_and_dicts(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(_TINY_PNG)
            path = str(Path(f.name).resolve())
        try:
            atts = ic.normalize_image_attachments(
                [path, {"path": path, "placeholder": "[Image #1]", "source": "clipboard"}]
            )
            self.assertEqual(len(atts), 2)
            self.assertEqual(atts[0]["path"], path)
            self.assertEqual(atts[1]["placeholder"], "[Image #1]")
            self.assertEqual(atts[1]["source"], "clipboard")
        finally:
            Path(path).unlink(missing_ok=True)

    def test_build_user_content_with_attachments(self):
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as f:
            f.write(_TINY_PNG)
            path = str(Path(f.name).resolve())
        try:
            content = ic.build_user_content_with_images("看图", images=[path])
        finally:
            Path(path).unlink(missing_ok=True)

        self.assertIsNotNone(content)
        self.assertEqual(content[0]["type"], "text")
        # O2: text keeps the original prompt and appends a source/size note.
        self.assertTrue(content[0]["text"].startswith("看图"))
        self.assertIn("[image] source:", content[0]["text"])
        self.assertEqual(content[1]["type"], "image")
        self.assertEqual(base64.b64decode(content[1]["source"]["data"]), _TINY_PNG)


class DisplayNoteAndResizeTest(unittest.TestCase):
    """O2/O4：尺寸说明、原图尺寸、面积上限、说明开关、失败收集。"""

    def setUp(self):
        ic.clear_encode_cache()

    def _make_image(self, width, height, suffix=".png"):
        from PIL import Image

        img = Image.new("RGB", (width, height), (30, 60, 90))
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as f:
            img.save(f, format="PNG" if suffix == ".png" else "JPEG")
            return Path(f.name)

    def _pillow_available(self):
        try:
            import PIL  # noqa: F401
            return True
        except Exception:
            return False

    def test_display_note_for_large_image_has_multiply_hint(self):
        if not self._pillow_available():
            self.skipTest("Pillow not installed")
        path = self._make_image(3024, 1964)
        try:
            enc = ic.encode_image_for_prompt(str(path))
        finally:
            path.unlink(missing_ok=True)
        self.assertTrue(enc.resized)
        self.assertEqual((enc.orig_width, enc.orig_height), (3024, 1964))
        self.assertEqual(max(enc.width, enc.height), ic.MAX_DIMENSION)
        note = ic.display_note(enc)
        self.assertIn("[image]", note)
        self.assertIn("original 3024x1964", note)
        self.assertIn("multiply coordinates by", note)

    def test_small_image_note_has_no_multiply_hint(self):
        if not self._pillow_available():
            self.skipTest("Pillow not installed")
        path = self._make_image(400, 300)
        try:
            enc = ic.encode_image_for_prompt(str(path))
        finally:
            path.unlink(missing_ok=True)
        self.assertFalse(enc.resized)
        note = ic.display_note(enc)
        self.assertIn("original 400x300", note)
        self.assertNotIn("multiply coordinates by", note)

    def test_area_cap_resizes_wide_strip(self):
        if not self._pillow_available():
            self.skipTest("Pillow not installed")
        path = self._make_image(4000, 500)
        try:
            enc = ic.encode_image_for_prompt(str(path))
        finally:
            path.unlink(missing_ok=True)
        self.assertTrue(enc.resized)
        self.assertLessEqual(enc.width * enc.height, ic.MAX_IMAGE_AREA)
        self.assertLessEqual(max(enc.width, enc.height), ic.MAX_DIMENSION)

    def test_notes_can_be_disabled_via_env(self):
        if not self._pillow_available():
            self.skipTest("Pillow not installed")
        path = self._make_image(400, 300)
        old = os.environ.get("GA_IMAGE_NOTES")
        try:
            os.environ["GA_IMAGE_NOTES"] = "0"
            content = ic.build_user_content_with_images("看图", images=[str(path)])
        finally:
            if old is None:
                os.environ.pop("GA_IMAGE_NOTES", None)
            else:
                os.environ["GA_IMAGE_NOTES"] = old
            path.unlink(missing_ok=True)
        self.assertNotIn("[image]", content[0]["text"])

    def test_failures_collected_for_bad_path(self):
        failures = []
        content = ic.build_user_content_with_images(
            "看图", images=[str(REPO_ROOT / "no_such_image_xyz.png")], failures=failures
        )
        self.assertIsNotNone(content)
        self.assertEqual(len(failures), 1)
        self.assertIn("no_such_image_xyz.png", failures[0]["path"])
        self.assertTrue(failures[0]["error"])



if __name__ == "__main__":
    unittest.main()
