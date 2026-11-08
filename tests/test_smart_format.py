"""smart_format 省略量可见性测试。"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from genericagent.core.handler import smart_format  # noqa: E402


class SmartFormatTest(unittest.TestCase):
    def test_short_input_is_unchanged(self):
        self.assertEqual(smart_format("abc", max_str_len=100), "abc")
        # Exactly at the boundary: no truncation, no note.
        exact = "x" * 100
        self.assertEqual(smart_format(exact, max_str_len=100), exact)

    def test_note_reports_the_real_omitted_count(self):
        data = "a" * 5000
        result = smart_format(data, max_str_len=100)
        # head(50) + note + tail(50); 4900 chars were dropped.
        self.assertIn("4,900 chars", result)
        self.assertIn("omitted", result)

    def test_keeps_head_and_tail(self):
        data = "HEAD" + "m" * 5000 + "TAIL"
        result = smart_format(data, max_str_len=100)
        self.assertTrue(result.startswith("HEAD"))
        self.assertTrue(result.endswith("TAIL"))

    def test_byte_count_differs_from_char_count_for_cjk(self):
        # Chinese is 3 bytes per char in UTF-8, so the byte count must be larger.
        data = "中" * 3000
        result = smart_format(data, max_str_len=100)
        chars = len(data) - 100
        self.assertIn(f"{chars:,} chars", result)
        self.assertIn(f"{chars * 3:,} bytes", result)

    def test_custom_omit_str_still_works(self):
        # Existing call sites pass their own wording; behaviour must not change.
        data = "x" * 5000
        result = smart_format(data, max_str_len=100, omit_str="\n\n[omitted long output]\n\n")
        self.assertIn("[omitted long output]", result)
        self.assertNotIn("chars /", result)

    def test_non_string_input_is_coerced(self):
        self.assertEqual(smart_format(42, max_str_len=100), "42")

    def test_zero_limit_returns_input_untouched(self):
        # A non-positive limit would otherwise produce a meaningless note.
        data = "x" * 500
        self.assertEqual(smart_format(data, max_str_len=0), data)


if __name__ == "__main__":
    unittest.main()
