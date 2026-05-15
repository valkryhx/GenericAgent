import os
import sys
import unittest
import asyncio
from unittest.mock import patch


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
FRONTENDS = os.path.join(ROOT, "frontends")
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)
if FRONTENDS not in sys.path:
    sys.path.insert(0, FRONTENDS)

from tuiapp_v2 import ChatMessage, GenericAgentTUI, InputArea, build_user_body, StreamUpdateGate


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


class InputAreaPasteTest(unittest.TestCase):
    def test_ctrl_v_binding_invokes_paste_action(self):
        actions = {binding.action for binding in InputArea.BINDINGS if str(binding.key) == "ctrl+v"}

        self.assertEqual({"paste"}, actions)

    def test_action_paste_folds_multiline_clipboard_text(self):
        inp = InputArea("")
        inserted = []
        text = "model_provider = \"deepseek\"\nmodel = \"gpt-5.5\"\napproval_policy = \"never\""

        def insert(text_to_insert):
            inserted.append(text_to_insert)

        inp._insert_via_keyboard = insert

        with patch("tuiapp_v2._read_clipboard_text", return_value=text):
            inp.action_paste()

        self.assertEqual(['[Copied text #1 +2 lines]'], inserted)
        self.assertEqual(text, inp.expand_placeholders(inserted[0]))

    def test_action_paste_folds_two_line_clipboard_text(self):
        inp = InputArea("")
        inserted = []
        text = "alpha\nbeta"
        inp._insert_via_keyboard = lambda text_to_insert: inserted.append(text_to_insert)

        with patch("tuiapp_v2._read_clipboard_text", return_value=text):
            inp.action_paste()

        self.assertEqual(["[Copied text #1 +1 lines]"], inserted)
        self.assertEqual(text, inp.expand_placeholders(inserted[0]))

    def test_linewise_terminal_paste_is_folded_without_submit(self):
        inp = InputArea("")
        inserted = []
        posted = []

        class Timer:
            def __init__(self, callback):
                self.callback = callback
                self.stopped = False

            def stop(self):
                self.stopped = True

        class Event:
            key = "enter"
            stopped = False
            prevented = False

            def stop(self):
                self.stopped = True

            def prevent_default(self):
                self.prevented = True

        inp.text = "first line"
        inp._insert_via_keyboard = lambda text: inserted.append(text)
        inp.post_message = lambda msg: posted.append(getattr(msg, "value", None))
        timers = []
        inp.set_timer = lambda _delay, callback: timers.append(Timer(callback)) or timers[-1]

        asyncio.run(inp._on_key(Event()))

        self.assertNotIn("pending", posted)
        self.assertEqual(1, len(timers))
        self.assertFalse(timers[0].stopped)
        self.assertTrue(hasattr(inp, "_pending_submit_timer"))

        inp._cancel_pending_submit_as_newline()

        self.assertTrue(timers[0].stopped)
        self.assertEqual(["\n"], inserted)
        self.assertIsNone(inp._pending_submit_timer)
        self.assertTrue(inp._linewise_paste_active)

        inp.text = "first line\nsecond line"
        inp._finalize_linewise_paste()

        self.assertEqual(["\n", "[Copied text #1 +1 lines]"], inserted)
        self.assertEqual("first line\nsecond line", inp.expand_placeholders("[Copied text #1 +1 lines]"))
        self.assertFalse([value for value in posted if value is not None])

    def test_enter_during_linewise_paste_adds_newline_without_submit(self):
        inp = InputArea("")
        posted = []
        inserted = []
        timers = []

        class Timer:
            def stop(self):
                pass

        class Event:
            key = "enter"

            def stop(self):
                pass

            def prevent_default(self):
                pass

        inp.text = "a\nb"
        inp._linewise_paste_active = True
        inp._insert_via_keyboard = lambda text: inserted.append(text)
        inp.set_timer = lambda _delay, callback: timers.append(Timer()) or timers[-1]
        inp.post_message = lambda msg: posted.append(getattr(msg, "value", None))

        asyncio.run(inp._on_key(Event()))

        self.assertEqual(["\n"], inserted)
        self.assertTrue(inp._linewise_paste_active)
        self.assertEqual(1, len(timers))
        self.assertFalse([value for value in posted if value is not None])

    def test_enter_inside_linewise_paste_keeps_collecting_until_timeout(self):
        inp = InputArea("")
        posted = []
        inserted = []

        class Timer:
            def __init__(self, callback):
                self.callback = callback
                self.stopped = False

            def stop(self):
                self.stopped = True

        class Event:
            key = "enter"
            stopped = False
            prevented = False

            def stop(self):
                self.stopped = True

            def prevent_default(self):
                self.prevented = True

        timers = []
        inp.text = "first\nsecond"
        inp._linewise_paste_active = True
        inp._insert_via_keyboard = lambda text: inserted.append(text)
        inp.set_timer = lambda _delay, callback: timers.append(Timer(callback)) or timers[-1]
        inp.post_message = lambda msg: posted.append(getattr(msg, "value", None))

        asyncio.run(inp._on_key(Event()))

        self.assertEqual(["\n"], inserted)
        self.assertTrue(inp._linewise_paste_active)
        self.assertEqual(1, len(timers))
        self.assertFalse([value for value in posted if value is not None])

        inp.text = "first\nsecond\nthird"
        inp._finalize_linewise_paste()

        self.assertEqual(["\n", "[Copied text #1 +2 lines]"], inserted)
        self.assertEqual("first\nsecond\nthird", inp.expand_placeholders("[Copied text #1 +2 lines]"))

    def test_placeholder_with_suffix_expands_on_submit_value(self):
        inp = InputArea("")
        text = "a\nb\nc"

        inp._insert_via_keyboard = lambda _text: None
        inp._insert_paste_text(text)

        self.assertEqual("a\nb\nc 请检查", inp.expand_placeholders("[Copied text #1 +2 lines] 请检查"))

    def test_reset_cancels_pending_submit_timer(self):
        inp = InputArea("")
        posted = []

        class Timer:
            stopped = False

            def stop(self):
                self.stopped = True

        timer = Timer()
        inp.text = "pending"
        inp._pending_submit_timer = timer
        inp.post_message = lambda msg: posted.append(getattr(msg, "value", None))

        inp.reset()

        self.assertTrue(timer.stopped)
        self.assertIsNone(inp._pending_submit_timer)
        self.assertNotIn("pending", posted)


class UserMessageRenderTest(unittest.TestCase):
    def test_toml_section_header_is_rendered_as_literal_text(self):
        body = build_user_body('[model_providers.deepseek]\nname = "deepseek"')

        self.assertIn("[model_providers.deepseek]", body.plain)
        self.assertIn('name = "deepseek"', body.plain)


if __name__ == "__main__":
    unittest.main()
