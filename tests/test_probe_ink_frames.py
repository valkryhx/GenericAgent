"""Tests for scripts/probe_ink_frames.py.

The script turns a transient terminal-frame bug into files on disk. These tests pin
the two pieces that must be right for that to work:

  * key-spec decoding -- a mistyped escape would silently send the wrong repro;
  * frame splitting -- if boundaries are wrong, a one-frame flash is merged into a
    neighbour and the bug disappears into the noise.

See ``docs/ga_ink_transient_frame_debug_2026-11-11.md``.
"""
from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_probe_module():
    path = REPO_ROOT / "scripts" / "probe_ink_frames.py"
    spec = importlib.util.spec_from_file_location("ga_probe_ink_frames", path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


probe = _load_probe_module()


class DecodeKeysTest(unittest.TestCase):
    def test_plain_text_is_one_group(self):
        self.assertEqual(["nnndadadad"], probe.decode_keys("nnndadadad"))

    def test_pipe_separates_groups(self):
        self.assertEqual(["a", "b", "c"], probe.decode_keys("a|b|c"))

    def test_escape_sequences_become_real_control_characters(self):
        # The reported repro is "type gibberish, Enter, Esc".
        self.assertEqual(["draft", "\r", "\x1b"], probe.decode_keys("draft|\\r|\\x1b"))

    def test_empty_groups_are_dropped(self):
        # A trailing pipe is easy to write by accident and must not send "".
        self.assertEqual(["a", "\r"], probe.decode_keys("a|\\r|"))

    def test_tab_and_newline_are_supported(self):
        self.assertEqual(["\t", "\n"], probe.decode_keys("\\t|\\n"))

    def test_literal_backslash_r_in_text_is_not_mangled(self):
        # A Windows path typed into the composer must survive. `\r` alone is Enter, so
        # a literal backslash is written as `\\` -- the standard convention, and the
        # reason decoding has to be a single left-to-right pass rather than a
        # sequence of str.replace calls (those would turn C:\repo into C:<CR>epo).
        self.assertEqual(["C:\\repo"], probe.decode_keys("C:\\\\repo"))

    def test_bare_backslash_r_still_means_enter(self):
        # The convenience the escape table exists for.
        self.assertEqual(["\r"], probe.decode_keys("\\r"))

    def test_unknown_escape_is_left_intact(self):
        # `\q` has no meaning; dropping the backslash would silently alter the text.
        self.assertEqual(["a\\qb"], probe.decode_keys("a\\qb"))

    def test_trailing_backslash_is_preserved(self):
        self.assertEqual(["dir\\"], probe.decode_keys("dir\\"))


class FrameSplittingTest(unittest.TestCase):
    def test_ansi_is_stripped_but_text_is_kept(self):
        raw = "\x1b[2J\x1b[H hello \x1b[?25hworld"
        self.assertEqual(" hello world", probe.strip_ansi(raw))

    def test_a_single_frame_stays_one_block(self):
        frame = "GenericAgent   idle\n >\n────\n"
        blocks = probe.split_frames(frame)
        self.assertEqual(1, len(blocks))
        self.assertIn("idle", blocks[0])

    def test_blank_line_runs_separate_frames(self):
        stream = "frame one\n\n\n\n\nframe two\n"
        blocks = probe.split_frames(stream)
        self.assertEqual(2, len(blocks))
        self.assertIn("one", blocks[0])
        self.assertIn("two", blocks[1])

    def test_a_transient_flash_survives_as_its_own_frame(self):
        """The whole point: a panel that paints once must not vanish into a neighbour.

        Real Ink output erases and repaints; the flash is one block with the panel
        text, surrounded by blocks that lack it.
        """
        stream = (
            "GenericAgent   idle\n >\n────\n"
            "\n\n\n\n"
            "Implementations\n  impl-b  wf_1-21b-1   Waiting\n  # [working] #2 review\n"
            "\n\n\n\n"
            "GenericAgent   idle\n >\n────\n"
        )
        blocks = probe.split_frames(stream)
        self.assertEqual(3, len(blocks), f"expected the flash to be isolated, got {blocks}")
        self.assertIn("impl-b", blocks[1])
        self.assertNotIn("impl-b", blocks[0])
        self.assertNotIn("impl-b", blocks[2])

    def test_empty_stream_yields_no_frames(self):
        self.assertEqual([], probe.split_frames(""))
        self.assertEqual([], probe.split_frames("\n\n\n\n"))

    def test_whitespace_only_blocks_are_dropped(self):
        self.assertEqual([], probe.split_frames("   \n\n\n\n   \n"))


class UiDirResolutionTest(unittest.TestCase):
    def test_explicit_valid_dir_is_used(self):
        resolved = probe.resolve_ui_dir(str(REPO_ROOT / "frontends" / "ink" / "ui"))
        self.assertEqual((REPO_ROOT / "frontends" / "ink" / "ui").resolve(), resolved)

    def test_missing_ui_raises_with_the_searched_paths(self):
        # A bad --ui-dir must fail loudly. Launching node against a missing entry
        # produces "0 frames", which reads exactly like "the bug did not reproduce".
        with self.assertRaises(FileNotFoundError) as ctx:
            probe.resolve_ui_dir("/nonexistent/definitely/not/here")
        message = str(ctx.exception)
        # Windows resolves and rewrites the separators, so assert on the segments.
        self.assertIn("definitely", message)
        self.assertIn("main.tsx", message)

    def test_explicit_dir_without_an_entry_point_is_rejected(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(FileNotFoundError):
                probe.resolve_ui_dir(td)

    def test_auto_resolution_finds_a_real_ui(self):
        # The checkout always has one, and the installed wheel usually does too.
        resolved = probe.resolve_ui_dir()
        self.assertTrue((resolved / "src" / "main.tsx").is_file())


if __name__ == "__main__":
    unittest.main()