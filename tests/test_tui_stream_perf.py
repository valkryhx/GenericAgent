import os
import sys
import unittest
from unittest.mock import patch


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
FRONTENDS = os.path.join(ROOT, "frontends")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if FRONTENDS not in sys.path:
    sys.path.insert(0, FRONTENDS)

from tuiapp_v2 import ChatMessage, GenericAgentTUI, StreamUpdateGate


class StreamUpdateGateTest(unittest.TestCase):
    def test_first_update_is_allowed(self):
        gate = StreamUpdateGate(min_interval=0.2, min_chars=80)

        self.assertTrue(gate.should_flush(10, now=1.0, done=False))

    def test_small_update_before_interval_is_coalesced(self):
        gate = StreamUpdateGate(min_interval=0.2, min_chars=80)
        gate.should_flush(10, now=1.0, done=False)

        self.assertFalse(gate.should_flush(40, now=1.05, done=False))

    def test_update_flushes_after_interval(self):
        gate = StreamUpdateGate(min_interval=0.2, min_chars=80)
        gate.should_flush(10, now=1.0, done=False)

        self.assertTrue(gate.should_flush(40, now=1.25, done=False))

    def test_update_flushes_after_enough_new_text(self):
        gate = StreamUpdateGate(min_interval=0.2, min_chars=80)
        gate.should_flush(10, now=1.0, done=False)

        self.assertTrue(gate.should_flush(95, now=1.05, done=False))

    def test_done_always_flushes(self):
        gate = StreamUpdateGate(min_interval=10.0, min_chars=1000)
        gate.should_flush(10, now=1.0, done=False)

        self.assertTrue(gate.should_flush(11, now=1.01, done=True))


class ChatMessageRenderStateTest(unittest.TestCase):
    def make_unmounted_app(self):
        app = object.__new__(GenericAgentTUI)
        app.fold_mode = True
        return app

    def test_streaming_messages_start_in_plain_render_mode(self):
        msg = ChatMessage("assistant", "partial", done=False)

        self.assertEqual("plain", msg.render_mode)

    def test_completed_messages_start_in_markdown_render_mode(self):
        msg = ChatMessage("assistant", "final", done=True)

        self.assertEqual("markdown", msg.render_mode)

    def test_streaming_body_does_not_run_fold_or_markdown_rendering(self):
        app = self.make_unmounted_app()
        msg = ChatMessage("assistant", "**LLM Running (Turn 1) ...**\npartial", done=False)

        with patch("tuiapp_v2.render_folded_text") as folded:
            body = app._build_assistant_body(msg)

        folded.assert_not_called()
        self.assertIn("partial", body.plain)

    def test_completed_body_can_run_fold_rendering(self):
        app = self.make_unmounted_app()
        msg = ChatMessage("assistant", "**LLM Running (Turn 1) ...**\nfinal", done=True)

        with patch("tuiapp_v2.render_folded_text", return_value="folded") as folded:
            app._build_assistant_body(msg)

        folded.assert_called_once()


if __name__ == "__main__":
    unittest.main()
