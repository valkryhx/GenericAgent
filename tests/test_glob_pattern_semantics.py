"""``file_glob`` must answer by *path*, the same way on both engines.

The reported symptom was "file_glob doesn't work". Two separate things were wrong:

1. **Classification** (fixed in `genericagent/core/permissions/policy.py` and two
   sibling tables): a read tool was classified as an unrecognised *mutating* one,
   so read-only profiles refused it and the reason string said `static_unknown`.
   That is covered by ``tests/test_read_tool_classification.py``.

2. **Pattern semantics** (this file). A directory-scoped pattern -- ``src/*.py``,
   the most natural way to say "the Python files under src" -- matched **nothing**
   whenever ripgrep was installed:

       pattern       ripgrep    python fallback
       src/*.py      0          3
       src/**/*.py   0          1
       **/src/*.py   2          0

   Both directions are wrong, and the two engines disagree, so the answer depended
   on whether ripgrep happened to be on PATH. ``src/*.py`` returned an empty list,
   which the tool reports as "no files matched" -- indistinguishable from the
   directory being empty. That reads as "the tool is broken".

Root cause, in one sentence each:

* **ripgrep** matches a glob against **each path component**, so a pattern with a
  ``/`` in it only ever matches a full path from the search root. Passing the
  search root as an argument makes rg match against the *absolute* path instead,
  which is why ``**/src/*.py`` worked and ``src/*.py`` did not.
* **the Python fallback** used ``fnmatch``, where ``*`` also crosses ``/``, so
  ``src/*.py`` silently matched ``src/sub/c.py`` and ``*`` meant "everything
  recursively".

The rule this file pins: **the returned paths are relative to the requested root,
and the pattern is matched against that same relative path.** Whatever engine runs,
the same pattern gives the same answer.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from genericagent.core import file_search  # noqa: E402
from genericagent.core.file_search import glob_files  # noqa: E402


def _build_tree(root: Path) -> None:
    (root / "src" / "sub").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "node_modules" / "pkg").mkdir(parents=True)
    (root / "src" / "a.py").write_text("a\n", encoding="utf-8")
    (root / "src" / "b.py").write_text("b\n", encoding="utf-8")
    (root / "src" / "sub" / "c.py").write_text("c\n", encoding="utf-8")
    (root / "src" / "notes.md").write_text("n\n", encoding="utf-8")
    (root / "docs" / "guide.md").write_text("g\n", encoding="utf-8")
    (root / "readme.md").write_text("r\n", encoding="utf-8")
    (root / "node_modules" / "pkg" / "index.js").write_text("x\n", encoding="utf-8")


class _Base(unittest.TestCase):
    engine_hint = ""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        _build_tree(self.root)
        self._patch = None
        if self.engine_hint == "python":
            # Force the fallback path: same contract, no ripgrep installed.
            self._patch = patch.object(file_search, "find_ripgrep", return_value=None)
            self._patch.start()

    def tearDown(self):
        if self._patch is not None:
            self._patch.stop()
        self._tmp.cleanup()

    def glob(self, pattern: str) -> list[str]:
        result = glob_files(str(self.root), pattern)
        self.assertFalse(result.note, f"unexpected note for {pattern!r}: {result.note}")
        return result.filenames


class GlobRipgrepEngineTest(_Base):
    engine_hint = "rg"

    def test_directory_scoped_pattern_matches_direct_children(self):
        # THE reported shape: "the Python files in src".
        self.assertEqual(["src/a.py", "src/b.py"], self.glob("src/*.py"))

    def test_directory_scoped_globstar_matches_nested(self):
        # `**/` matches ZERO or more directories (bash globstar, pathlib), so
        # `src/**/*.py` includes the direct children too. Getting this wrong in the
        # other direction is how `src/**/*.py` came to mean "only subdirectories".
        self.assertEqual(["src/a.py", "src/b.py", "src/sub/c.py"], self.glob("src/**/*.py"))

    def test_leading_globstar_is_tolerated(self):
        # A model that saw ripgrep's component semantics will write this instead.
        # It must mean the same thing, not something else.
        self.assertEqual(["src/a.py", "src/b.py"], self.glob("**/src/*.py"))

    def test_a_bare_extension_pattern_is_recursive(self):
        # Documented, tested behaviour: `*.py` is "every .py under the root".
        self.assertEqual(["src/a.py", "src/b.py", "src/sub/c.py"], self.glob("*.py"))

    def test_a_star_does_not_cross_a_separator(self):
        # `src/*.py` must not include `src/sub/c.py`.
        self.assertNotIn("src/sub/c.py", self.glob("src/*.py"))

    def test_dependency_directories_stay_excluded(self):
        self.assertEqual([], self.glob("*.js"))

    def test_a_pattern_matching_nothing_is_empty_not_an_error(self):
        self.assertEqual([], self.glob("nope/*.py"))

    def test_paths_are_relative_to_the_requested_root(self):
        for path in self.glob("**/*.md"):
            self.assertFalse(Path(path).is_absolute(), f"{path} should be relative")
            self.assertNotIn("\\", path, f"{path} should use forward slashes")

    def test_results_are_sorted_and_unique(self):
        self.assertEqual(sorted(set(self.glob("*.py"))), self.glob("*.py"))


class GlobPythonFallbackTest(_Base):
    """The fallback must honour the SAME contract, not `fnmatch` semantics.

    ``fnmatch`` treats ``*`` as crossing ``/``, which made ``src/*.py`` silently
    match nested files and made the two engines disagree on every scoped pattern.
    """

    engine_hint = "python"

    def test_directory_scoped_pattern_matches_only_direct_children(self):
        self.assertEqual(["src/a.py", "src/b.py"], self.glob("src/*.py"))

    def test_directory_scoped_globstar_matches_nested(self):
        # `**/` matches zero or more directories, so direct children are included.
        self.assertEqual(["src/a.py", "src/b.py", "src/sub/c.py"], self.glob("src/**/*.py"))

    def test_leading_globstar_is_tolerated(self):
        self.assertEqual(["src/a.py", "src/b.py"], self.glob("**/src/*.py"))

    def test_a_bare_extension_pattern_is_recursive(self):
        self.assertEqual(["src/a.py", "src/b.py", "src/sub/c.py"], self.glob("*.py"))

    def test_dependency_directories_stay_excluded(self):
        self.assertEqual([], self.glob("*.js"))


class GlobEngineParityTest(unittest.TestCase):
    """Whatever the engine, the same pattern returns the same answer.

    This is the test that would have caught the bug: it never asserts a hardcoded
    expected list, only that the two engines agree.
    """

    PATTERNS = (
        "*.py",
        "*.md",
        "src/*.py",
        "src/**/*.py",
        "**/src/*.py",
        "**/*.md",
        "src/sub/*.py",
        "nope/*.py",
    )

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name).resolve()
        _build_tree(self.root)

    def tearDown(self):
        self._tmp.cleanup()

    def test_both_engines_agree_on_every_pattern(self):
        with_rg = {pattern: glob_files(str(self.root), pattern).filenames for pattern in self.PATTERNS}
        with patch.object(file_search, "find_ripgrep", return_value=None):
            without_rg = {pattern: glob_files(str(self.root), pattern).filenames for pattern in self.PATTERNS}
        mismatches = {
            pattern: (with_rg[pattern], without_rg[pattern])
            for pattern in self.PATTERNS
            if with_rg[pattern] != without_rg[pattern]
        }
        self.assertEqual(
            {},
            mismatches,
            "engines disagree; a user's result would depend on whether ripgrep is installed",
        )


class GlobEmptyPatternTest(_Base):
    def test_an_empty_pattern_returns_a_note_instead_of_listing_the_tree(self):
        # ``rg --glob ""`` lists every file, so an empty pattern used to mean "dump
        # the whole tree". A caller that computed a pattern and got an empty string
        # must be told so, not handed a repository listing it will read as results.
        result = glob_files(str(self.root), "")
        self.assertEqual([], result.filenames)
        self.assertIn("requires", result.note)
        self.assertIn("pattern", result.note)


if __name__ == "__main__":
    unittest.main()