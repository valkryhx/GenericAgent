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


class TraceFileParsingTest(unittest.TestCase):
    """The --attach path reads traces written by scripts/ga_frame_trace.mjs.

    Framing is byte-length based, so these tests pin the property that makes that
    worth doing: a chunk containing newlines, ANSI sequences, or multi-byte UTF-8 must
    come back exactly, and the frame after it must still line up.
    """

    def _write(self, directory, name, chunks, pid=4242):
        path = Path(directory) / name
        blob = f"=== GA_TRACE 2026-11-11T00:00:00.000Z pid={pid} cwd=/tmp out=/tmp ===\n".encode()
        for seq, chunk in enumerate(chunks, start=1):
            raw = chunk.encode("utf-8")
            head = (
                f"\n=== GA_FRAME 2026-11-11T00:00:0{seq}.000Z #{seq} "
                f"cols=80 rows=24 bytes={len(raw)} ===\n"
            ).encode()
            blob += head + raw
        path.write_bytes(blob)
        return path

    def test_frames_come_back_in_order(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write(td, "frames-1.log", ["first", "second"])
            frames = probe.read_trace_file(path)
        self.assertEqual([1, 2], [f["seq"] for f in frames])
        self.assertEqual(["first", "second"], [f["raw"] for f in frames])

    def test_newlines_and_ansi_inside_a_chunk_do_not_split_it(self):
        # The naive "split on blank lines" approach fails here: this is ONE write that
        # happens to contain a full screen.
        chunk = "\x1b[2Jline one\n\n\n\nline five\n >\n"
        with tempfile.TemporaryDirectory() as td:
            frames = probe.read_trace_file(self._write(td, "frames-1.log", [chunk, "next"]))
        self.assertEqual(2, len(frames))
        self.assertEqual(chunk, frames[0]["raw"])

    def test_multibyte_utf8_does_not_shift_later_frames(self):
        # Byte counts, not character counts: '中文' is 6 bytes and 2 chars. Parsing the
        # header against a decoded string would desync every following frame.
        with tempfile.TemporaryDirectory() as td:
            frames = probe.read_trace_file(
                self._write(td, "frames-1.log", ["中文", "\x1b[1m面板 Waiting\x1b[0m"])
            )
        self.assertEqual("中文", frames[0]["raw"])
        self.assertIn("面板 Waiting", frames[1]["raw"])

    def test_a_partial_final_write_is_flagged_not_swallowed(self):
        # Killing ga mid-write leaves a truncated tail; it must still surface the frames
        # before it rather than discarding the whole file.
        with tempfile.TemporaryDirectory() as td:
            path = self._write(td, "frames-1.log", ["complete", "will be cut"])
            data = path.read_bytes()
            path.write_bytes(data[:-5])
            frames = probe.read_trace_file(path)
        self.assertEqual("complete", frames[0]["raw"])
        self.assertTrue(frames[1]["truncated"])

    def test_a_file_with_no_frames_yields_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "frames-1.log"
            path.write_bytes(b"=== GA_TRACE ... ===\nargv=[]\n")
            self.assertEqual([], probe.read_trace_file(path))

    def test_grep_matches_only_frames_with_the_text(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write(td, "frames-1.log", ["idle", "impl-b Waiting", "idle again"])
            frames = probe.read_trace_file(path)
        hits = [probe.render_frame(path, frame, "Waiting") for frame in frames]
        printed = [text for text in hits if text]
        self.assertEqual(1, len(printed))
        self.assertIn("impl-b", printed[0])

    def test_find_traces_ignores_unrelated_files(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "notes.txt").write_text("x")
            self._write(td, "frames-99.log", ["a"])
            found = [p.name for p in probe.find_traces(Path(td))]
        self.assertEqual(["frames-99.log"], found)


class TracePreloadScriptTest(unittest.TestCase):
    """Static checks on ga_frame_trace.mjs -- it has no Python-importable surface.

    These pin the two decisions that were wrong on the first attempt and produced
    unusable output: recording every child node process, and framing by blank lines.
    """

    def setUp(self):
        self.source = (REPO_ROOT / "scripts" / "ga_frame_trace.mjs").read_text(encoding="utf-8")

    def test_it_skips_non_ui_child_processes_by_default(self):
        # NODE_OPTIONS leaks into MCP servers and npm helpers; each one wrote its own
        # trace file, burying the real UI among 10+ stubs.
        self.assertIn("looksLikeInkUi", self.source)
        self.assertIn("GA_UI_TRACE_ALL", self.source)

    def test_it_frames_by_byte_length(self):
        # Blank-line framing shatters any screen containing an empty region.
        self.assertIn("bytes=", self.source)
        self.assertIn("Buffer.from", self.source)

    def test_it_does_nothing_when_disabled(self):
        self.assertIn("if (!raw || raw === '0') return", self.source)

    def test_it_preserves_tty_transparency(self):
        # Replacing stdout with a plain object would flip ink onto its non-TTY path,
        # making the recorded frames differ from what the user actually sees.
        self.assertIn("new Proxy", self.source)
        self.assertIn("isTTY", self.source)


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


class TimelineTest(unittest.TestCase):
    """``--timeline`` answers "when did it appear?" -- the step that ends most hunts.

    It exists because doing this by hand was a throwaway script every time, and a
    step that costs a script to set up is a step that gets skipped.
    """

    def _frame(self, seq, body, iso="2026-11-11T00:00:00.000Z"):
        return {"seq": seq, "iso": iso, "cols": 80, "rows": 24, "raw": body, "truncated": False}

    def test_a_flash_that_returns_is_two_present_runs(self):
        frames = [
            self._frame(1, "idle\n >\n"),
            self._frame(2, "panel impl-b Waiting\n"),
            self._frame(3, "idle\n >\n"),
            self._frame(4, "panel impl-b Waiting\n"),
        ]
        runs = probe.build_timeline(frames, "Waiting")
        self.assertEqual([False, True, False, True], [run["present"] for run in runs])

    def test_blank_cursor_park_frames_do_not_split_a_run(self):
        # ink writes cursor-park sequences that strip to nothing. Counting those as
        # frames would break every run into pieces and make the table unreadable.
        frames = [
            self._frame(1, "panel impl-b Waiting\n"),
            self._frame(2, "\x1b[3A\x1b[5G\x1b[?25h"),
            self._frame(3, "panel impl-b Waiting\n"),
        ]
        runs = probe.build_timeline(frames, "Waiting")
        self.assertEqual(1, len(runs))
        self.assertEqual([1, 3], [runs[0]["first_seq"], runs[0]["last_seq"]])

    def test_an_absent_run_has_no_sample(self):
        runs = probe.build_timeline([self._frame(1, "idle\n")], "Waiting")
        self.assertEqual("", runs[0]["sample"])

    def test_the_sample_shows_what_reappeared(self):
        runs = probe.build_timeline(
            [self._frame(1, "  ok Subagent workflow-child:wf_1:agent_1 completed\n")],
            "workflow-child",
        )
        self.assertIn("agent_1", runs[0]["sample"])

    def test_reappearance_is_called_out_explicitly(self):
        # The shape IS the finding; leaving the reader to spot it is how a report
        # gets skimmed and the wrong conclusion drawn.
        frames = [
            self._frame(1, "x Waiting\n"),
            self._frame(2, "idle\n"),
            self._frame(3, "x Waiting\n"),
        ]
        text = probe.format_timeline(probe.build_timeline(frames, "Waiting"), "Waiting")
        self.assertIn("再出现", text)

    def test_never_disappearing_reads_differently_from_reappearing(self):
        frames = [self._frame(1, "x Waiting\n"), self._frame(2, "x Waiting\n")]
        text = probe.format_timeline(probe.build_timeline(frames, "Waiting"), "Waiting")
        self.assertNotIn("再出现", text)
        self.assertIn("一直未消失", text)

    def test_a_single_appearance_is_reported_as_such(self):
        frames = [self._frame(1, "idle\n"), self._frame(2, "x Waiting\n")]
        text = probe.format_timeline(probe.build_timeline(frames, "Waiting"), "Waiting")
        self.assertIn("只出现过一次", text)

    def test_an_empty_trace_says_so_instead_of_printing_nothing(self):
        self.assertIn("没有可用帧", probe.format_timeline([], "Waiting"))

    def test_resolve_trace_paths_rejects_a_missing_path(self):
        with self.assertRaises(FileNotFoundError):
            probe.resolve_trace_paths("/nonexistent/definitely/not/here")


if __name__ == "__main__":
    unittest.main()


