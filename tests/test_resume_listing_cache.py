import json
import re
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import session_transcript
from frontends import continue_cmd


def write_native_log(path, user_text="hello", assistant_text="hi"):
    prompt = {"role": "user", "content": [{"type": "text", "text": user_text}]}
    response = [{"type": "text", "text": assistant_text}]
    Path(path).write_text(
        "=== Prompt === 2026-05-23 13:00:00\n"
        + json.dumps(prompt, ensure_ascii=False, indent=2)
        + "\n\n=== Response === 2026-05-23 13:00:01\n"
        + repr(response)
        + "\n\n",
        encoding="utf-8",
    )


def record_turn(path, session_id, turn_id, user_text, assistant_text, before, after):
    session_transcript.record_turn(
        path,
        session_id=session_id,
        turn_id=turn_id,
        source="user",
        user_text=user_text,
        assistant_text=assistant_text,
        backend_history_before=before,
        backend_history_after=after,
    )


class TranscriptListingCacheTest(unittest.TestCase):
    def _root_with_two_sessions(self, tmp):
        root = Path(tmp)
        first = session_transcript.create_session(root=root, cwd="C:/repo", session_id="session_one")
        second = session_transcript.create_session(root=root, cwd="C:/repo", session_id="session_two")
        record_turn(first, "session_one", 1, "first question", "first answer", [],
                    [{"role": "user", "content": "first question"}])
        record_turn(second, "session_two", 1, "second question", "second answer", [],
                    [{"role": "user", "content": "second question"}])
        return root, first, second

    def test_second_listing_reuses_cache_instead_of_rescanning(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _, _ = self._root_with_two_sessions(tmp)
            with patch.object(session_transcript, "_scan_session_summary",
                              wraps=session_transcript._scan_session_summary) as scan:
                first = session_transcript.list_sessions(root=root)
                scanned_first = scan.call_count
                second = session_transcript.list_sessions(root=root)

            self.assertEqual(2, scanned_first, "each session file is scanned once")
            self.assertEqual(scanned_first, scan.call_count,
                             "a warm listing must not re-scan unchanged files")
            self.assertEqual(
                [(s.session_id, s.preview, s.rounds) for s in first],
                [(s.session_id, s.preview, s.rounds) for s in second],
            )
            self.assertTrue((root / ".listing_cache.json").is_file())

    def test_appending_a_turn_invalidates_only_that_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, first, second = self._root_with_two_sessions(tmp)
            session_transcript.list_sessions(root=root)
            record_turn(first, "session_one", 2, "follow up", "second answer",
                        [{"role": "user", "content": "first question"}],
                        [{"role": "user", "content": "first question"},
                         {"role": "assistant", "content": "first answer"}])
            with patch.object(session_transcript, "_scan_session_summary",
                              wraps=session_transcript._scan_session_summary) as scan:
                sessions = session_transcript.list_sessions(root=root)

            self.assertEqual(1, scan.call_count, "only the appended file is re-scanned")
            by_id = {s.session_id: s for s in sessions}
            self.assertEqual(2, by_id["session_one"].rounds)
            self.assertEqual(1, by_id["session_two"].rounds)

    def test_summary_matches_full_load_for_rewind_and_inferred_rewind(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = session_transcript.create_session(root=tmp, cwd="C:/repo", session_id="session_rw")
            after_one = [{"role": "user", "content": "one"}]
            after_two = after_one + [{"role": "assistant", "content": "a2"}]
            record_turn(path, "session_rw", 1, "one", "a1", [], after_one)
            record_turn(path, "session_rw", 2, "two", "a2", after_one, after_two)
            # 推断式 rewind：新 turn 的 before 指回第一个 turn 的 after
            record_turn(path, "session_rw", 3, "rewritten", "a3", after_one,
                        after_one + [{"role": "user", "content": "rewritten"}])
            session_transcript.record_rewind(path, session_id="session_rw", keep_turns=1,
                                             backend_history_after=after_one)
            record_turn(path, "session_rw", 4, "after rewind", "a4", after_one,
                        after_one + [{"role": "user", "content": "after rewind"}])

            loaded = session_transcript.load_session(path)
            summary = session_transcript.list_sessions(root=tmp)[0]

            self.assertEqual(loaded.preview, summary.preview)
            self.assertEqual(loaded.rounds, summary.rounds)
            self.assertEqual(loaded.last_seq, summary.last_seq)
            self.assertEqual(
                [t.user_text.strip() for t in loaded.turns if t.user_text.strip()],
                summary.user_texts,
            )

    def test_corrupt_cache_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as tmp:
            root, _, _ = self._root_with_two_sessions(tmp)
            (root / ".listing_cache.json").write_text("{not json", encoding="utf-8")
            sessions = session_transcript.list_sessions(root=root)
            self.assertEqual(2, len(sessions))
            self.assertEqual(["session_two", "session_one"], [s.session_id for s in sessions])


class LegacyListingCacheTest(unittest.TestCase):
    def test_second_listing_reuses_cache_and_appended_log_is_rescanned(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp) / "model_responses"
            log_dir.mkdir()
            log = log_dir / "model_responses_111111.txt"
            write_native_log(log, "legacy question", "legacy answer")
            with (
                patch.object(continue_cmd, "_LOG_GLOB", str(log_dir / "model_responses_*.txt")),
                patch.object(continue_cmd, "_SESSION_ROOT", str(Path(tmp) / "empty_sessions")),
                patch.object(continue_cmd, "_scan_legacy_summary",
                             wraps=continue_cmd._scan_legacy_summary) as scan,
            ):
                first = continue_cmd.list_sessions()
                scanned_first = scan.call_count
                second = continue_cmd.list_sessions()

                self.assertEqual(1, scanned_first)
                self.assertEqual(scanned_first, scan.call_count, "warm listing must not re-scan the log")
                self.assertEqual(first, second)
                self.assertEqual("legacy question", second[0][2])
                self.assertTrue((log_dir / ".listing_cache.json").is_file())

                with log.open("a", encoding="utf-8") as fh:
                    fh.write("=== Prompt === 2026-05-23 13:01:00\n")
                    fh.write(json.dumps({"role": "user", "content": [{"type": "text", "text": "second"}]}))
                    fh.write("\n\n=== Response === 2026-05-23 13:01:01\n")
                    fh.write(repr([{"type": "text", "text": "second answer"}]))
                    fh.write("\n\n")
                after_append = continue_cmd.list_sessions()

            self.assertEqual(1, len(after_append))
            self.assertEqual("legacy question", after_append[0][2])
            self.assertEqual(2, after_append[0][3], "the appended pair must show up as a second round")

    def test_unparseable_log_is_cached_as_empty_and_not_rescanned(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_dir = Path(tmp) / "model_responses"
            log_dir.mkdir()
            log = log_dir / "model_responses_222222.txt"
            log.write_text("just some text without markers\n", encoding="utf-8")
            with (
                patch.object(continue_cmd, "_LOG_GLOB", str(log_dir / "model_responses_*.txt")),
                patch.object(continue_cmd, "_SESSION_ROOT", str(Path(tmp) / "empty_sessions")),
                patch.object(continue_cmd, "_scan_legacy_summary",
                             wraps=continue_cmd._scan_legacy_summary) as scan,
            ):
                self.assertEqual([], continue_cmd.list_sessions())
                scanned_first = scan.call_count
                self.assertEqual([], continue_cmd.list_sessions())

            self.assertEqual(1, scanned_first)
            self.assertEqual(scanned_first, scan.call_count,
                             "a log with no parseable pairs must not be re-read")


class PairSplitterEquivalenceTest(unittest.TestCase):
    """`_pairs` 换成 re.split 后必须与旧的惰性正则逐字节等价。"""

    def test_split_matches_legacy_regex(self):
        bs = chr(92)
        legacy = re.compile(r'^=== (Prompt|Response) ===.*?' + bs + 'n(.*?)(?=^=== (?:Prompt|Response) ===|' + bs + 'Z)',
                            re.DOTALL | re.MULTILINE)
        content = (
            "=== Prompt === 2026-05-23 13:00:00\n"
            + json.dumps({"role": "user", "content": [{"type": "text", "text": "one"}]})
            + "\n\n=== Response === 2026-05-23 13:00:01\n"
            + repr([{"type": "text", "text": "a1"}])
            + "\n\n=== Prompt === 2026-05-23 13:00:02\n"
            + json.dumps({"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "r"}]})
            + "\n\n=== Response === 2026-05-23 13:00:03\n"
            + repr([{"type": "text", "text": "a2"}])
            + "\n\n"
        )
        blocks, pending, expected = legacy.findall(content), None, []
        for label, body in blocks:
            if label == 'Prompt':
                pending = body.strip()
            elif pending is not None:
                expected.append((pending, body.strip()))
                pending = None

        self.assertEqual(expected, continue_cmd._pairs(content))
        self.assertEqual([], continue_cmd._pairs(""))
        self.assertEqual(expected, continue_cmd._pairs(content))


if __name__ == "__main__":
    unittest.main()
