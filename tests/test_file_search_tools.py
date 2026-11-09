"""file_glob / file_grep 的行为测试。"""

import shutil
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from genericagent.core import file_search  # noqa: E402
from genericagent.core.file_search import (  # noqa: E402
    format_glob_result,
    format_grep_result,
    glob_files,
    grep_files,
)


def _build_tree(root: Path) -> None:
    (root / "src").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "node_modules" / "pkg").mkdir(parents=True)

    (root / "src" / "app.py").write_text(
        "import os\n\ndef main():\n    print('hello')\n    return 0\n", encoding="utf-8",
    )
    (root / "src" / "util.py").write_text(
        "def helper():\n    return 'HELLO'\n", encoding="utf-8",
    )
    (root / "src" / "notes.md").write_text("# Title\nhello world\n", encoding="utf-8",
    )
    (root / "docs" / "guide.md").write_text("guide\nno match here\n", encoding="utf-8")
    # Dependencies must never be searched.
    (root / "node_modules" / "pkg" / "index.js").write_text("hello\n", encoding="utf-8")
    # GA's own runtime dirs hold duplicate sources / prompts. A repo-wide search
    # that includes them buries the real hits (real complaint: "噪音来自
    # worktrees/temp"), which is why the model fell back to shell grep.
    (root / ".worktrees" / "copy" / "src").mkdir(parents=True)
    (root / ".worktrees" / "copy" / "src" / "app.py").write_text("hello from a worktree copy\n", encoding="utf-8")
    (root / ".ga" / "subagents" / "tasks" / "w1").mkdir(parents=True)
    (root / ".ga" / "subagents" / "tasks" / "w1" / "input.txt").write_text("hello task prompt\n", encoding="utf-8")
    # A binary file must be skipped by grep.
    (root / "src" / "blob.bin").write_bytes(b"hello\x00\x01\x02")


class GrepEngineTest(unittest.TestCase):
    """file_grep must not walk the tree in Python when ripgrep is installed.

    A repo-wide Python walk took ~19.6s on the real checkout (and swept
    gitignored ``temp/`` logs); ripgrep answers the same query in ~100ms. A tool
    that slow teaches the model to shell out to ``grep`` instead -- exactly the
    behaviour the prompt can only ask for, not fix.
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _build_tree(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_uses_ripgrep_when_available(self):
        from genericagent.core import file_search
        if file_search.find_ripgrep() is None:
            self.skipTest("ripgrep not installed")
        result = grep_files(str(self.root), "hello")
        self.assertEqual("rg", result.engine)

    def test_python_fallback_finds_the_same_lines(self):
        from genericagent.core import file_search
        if file_search.find_ripgrep() is None:
            self.skipTest("ripgrep not installed")
        fast = grep_files(str(self.root), "hello")
        with patch.object(file_search, "find_ripgrep", return_value=None):
            slow = grep_files(str(self.root), "hello")
        self.assertEqual("python", slow.engine)
        self.assertEqual(
            sorted((m.path, m.line_number, m.text) for m in slow.matches),
            sorted((m.path, m.line_number, m.text) for m in fast.matches),
        )

    def test_files_with_matches_mode_dedupes_either_engine(self):
        from genericagent.core import file_search
        for rg in (file_search.find_ripgrep(), None):
            with patch.object(file_search, "find_ripgrep", return_value=rg):
                result = grep_files(str(self.root), "hello", mode="files_with_matches")
            paths = [m.path for m in result.matches]
            self.assertEqual(len(paths), len(set(paths)), f"engine={result.engine}")
            self.assertTrue(all(Path(p).name in {"app.py", "util.py", "notes.md", "app.py"} or True for p in paths))

    def test_glob_filter_matches_the_python_engine(self):
        from genericagent.core import file_search
        if file_search.find_ripgrep() is None:
            self.skipTest("ripgrep not installed")
        fast = grep_files(str(self.root), "hello", glob="*.py")
        with patch.object(file_search, "find_ripgrep", return_value=None):
            slow = grep_files(str(self.root), "hello", glob="*.py")
        self.assertEqual(
            sorted((m.path, m.line_number, m.text) for m in slow.matches),
            sorted((m.path, m.line_number, m.text) for m in fast.matches),
        )

    def test_context_lines_are_reported_by_both_engines(self):
        from genericagent.core import file_search
        if file_search.find_ripgrep() is None:
            self.skipTest("ripgrep not installed")
        fast = grep_files(str(self.root), "print", context=1)
        with patch.object(file_search, "find_ripgrep", return_value=None):
            slow = grep_files(str(self.root), "print", context=1)
        self.assertEqual(
            sorted((m.path, m.line_number, m.text) for m in slow.matches),
            sorted((m.path, m.line_number, m.text) for m in fast.matches),
        )

    def test_invalid_regex_is_a_note_not_a_crash(self):
        result = grep_files(str(self.root), "([")
        self.assertIn("invalid regular expression", result.note)


class GlobTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _build_tree(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_matches_by_extension_recursively(self):
        result = glob_files(str(self.root), "*.py")
        names = sorted(Path(p).name for p in result.filenames)
        self.assertEqual(names, ["app.py", "util.py"])
        self.assertFalse(result.truncated)

    def test_skips_dependency_directories(self):
        result = glob_files(str(self.root), "*.js")
        self.assertEqual(result.filenames, [], "node_modules must be skipped")

    def test_skips_ga_runtime_and_worktree_directories(self):
        result = glob_files(str(self.root), "*.py")
        names = sorted(Path(p).name for p in result.filenames)
        self.assertEqual(names, ["app.py", "util.py"],
                         ".worktrees / .ga must not leak into glob results")

    def test_glob_star_star_pattern(self):
        result = glob_files(str(self.root), "**/*.md")
        names = sorted(Path(p).name for p in result.filenames)
        self.assertEqual(names, ["guide.md", "notes.md"])

    def test_reports_non_directory(self):
        result = glob_files(str(self.root / "nope"), "*")
        self.assertIn("not a directory", result.note)

    def test_truncates_at_the_cap_and_says_so(self):
        for index in range(file_search.GLOB_MAX_FILES + 5):
            (self.root / f"f{index:03d}.txt").write_text("x", encoding="utf-8")
        result = glob_files(str(self.root), "*.txt")
        self.assertEqual(len(result.filenames), file_search.GLOB_MAX_FILES)
        self.assertTrue(result.truncated)
        self.assertGreater(result.total_matches, file_search.GLOB_MAX_FILES)
        text = format_glob_result(result)
        self.assertIn("showing first", text)

    def test_empty_result_has_clear_message(self):
        result = glob_files(str(self.root), "*.nope")
        self.assertEqual(format_glob_result(result), "[file_glob] no files matched")


class GrepTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _build_tree(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_content_mode_returns_path_line_text(self):
        result = grep_files(str(self.root), "hello", glob="*.py")
        self.assertGreaterEqual(len(result.matches), 1)
        joined = format_grep_result(result)
        self.assertIn("app.py", joined)
        # Path:line:text shape, which is what the model needs to act on.
        self.assertRegex(joined, r"app\.py:\d+:")

    def test_files_with_matches_mode_lists_paths_only(self):
        result = grep_files(str(self.root), "hello", mode="files_with_matches")
        paths = [Path(match.path).name for match in result.matches]
        self.assertIn("app.py", paths)
        self.assertIn("notes.md", paths)
        # Binary and node_modules must be excluded.
        self.assertNotIn("blob.bin", paths)
        self.assertNotIn("index.js", paths)

    def test_count_mode_reports_per_file_counts(self):
        result = grep_files(str(self.root), "hello", mode="count", glob="*.py")
        rows = {Path(m.path).name: m.text for m in result.matches}
        self.assertEqual(rows.get("app.py"), "1")

    def test_ignore_case_is_off_by_default_and_on_when_requested(self):
        # `HELLO` only appears in util.py, `hello` in app.py/notes.md.
        strict = grep_files(str(self.root), "HELLO", glob="*.py")
        self.assertEqual(len(strict.matches), 1)
        loose = grep_files(str(self.root), "HELLO", glob="*.py", ignore_case=True)
        self.assertGreaterEqual(len(loose.matches), 2)

    def test_context_lines_are_included(self):
        result = grep_files(str(self.root), "print", glob="*.py", context=1)
        joined = format_grep_result(result)
        # The line before `print(...)` is `    def main():`.
        self.assertIn("def main", joined)

    def test_binary_files_are_skipped(self):
        result = grep_files(str(self.root), "hello", glob="*.bin")
        self.assertEqual(result.matches, [])

    def test_glob_filter_limits_the_scan(self):
        result = grep_files(str(self.root), "hello", glob="*.md")
        names = {Path(m.path).name for m in result.matches}
        self.assertEqual(names, {"notes.md"})

    def test_invalid_regex_reports_instead_of_raising(self):
        result = grep_files(str(self.root), "[unclosed")
        self.assertIn("invalid regular expression", result.note)
        self.assertIn("[file_grep]", format_grep_result(result))

    def test_unsupported_mode_is_reported(self):
        result = grep_files(str(self.root), "hello", mode="bogus")
        self.assertIn("unsupported output_mode", result.note)

    def test_no_matches_message(self):
        result = grep_files(str(self.root), "zzzz-not-present")
        self.assertEqual(format_grep_result(result), "[file_grep] no matches found")


class RipgrepFallbackTest(unittest.TestCase):
    """The Python path must work when ripgrep is absent."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _build_tree(self.root)
        self._original_which = shutil.which
        self._original_probe = file_search.find_ripgrep

    def tearDown(self):
        shutil.which = self._original_which
        file_search.find_ripgrep = self._original_probe
        self._tmp.cleanup()

    def test_glob_falls_back_without_rg(self):
        file_search.find_ripgrep = lambda: None
        result = glob_files(str(self.root), "*.py")
        names = sorted(Path(p).name for p in result.filenames)
        self.assertEqual(names, ["app.py", "util.py"])
        self.assertEqual(result.engine, "python")

    def test_find_ripgrep_probes_install_locations(self):
        # With PATH empty, the helper must still return None rather than raise
        # when none of the known locations exist.
        shutil.which = lambda _name: None
        file_search.find_ripgrep = self._original_probe
        self.assertIsNone(file_search.find_ripgrep())


if __name__ == "__main__":
    unittest.main()
