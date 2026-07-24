import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from agentmain import normalize_display_assistant_text, should_flush_display_delta  # noqa: E402


class AgentMainStreamingTest(unittest.TestCase):
    def test_flushes_small_streaming_deltas_before_large_buffer_builds(self):
        self.assertFalse(should_flush_display_delta("x" * 15, 0, "x" * 15))
        self.assertTrue(should_flush_display_delta("x" * 16, 0, "x" * 16))

    def test_flushes_turn_marker_immediately(self):
        self.assertTrue(
            should_flush_display_delta(
                "\n\n**LLM Running (Turn 1) ...**\n\n",
                0,
                "\n\n**LLM Running (Turn 1) ...**\n\n",
            )
        )

    def test_normalize_display_assistant_text_is_idempotent_for_summary(self):
        raw = "<summary>need clock</summary>\nbody"
        once = normalize_display_assistant_text(raw)
        twice = normalize_display_assistant_text(once)
        self.assertEqual(once, twice)
        self.assertIn("</summary>\n\n", once)

    def test_normalize_display_assistant_text_wraps_file_content_once(self):
        raw = "<file_content>hello</file_content>"
        once = normalize_display_assistant_text(raw)
        twice = normalize_display_assistant_text(once)
        self.assertEqual(once, twice)
        self.assertEqual(once.count("````"), 2)


if __name__ == "__main__":
    unittest.main()
