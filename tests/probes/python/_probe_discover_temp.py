"""Probe: the documented test command must run tests/__init__.py.

``tests/__init__.py`` redirects the temp dir to ``D:\\tmp``. Two facts decide
whether the documented command is honest, and both are measured here rather
than reasoned about:

1. ``python -m unittest discover -s tests -t .`` imports the ``tests`` package
   and therefore runs the redirect. Plain ``discover -s tests`` does not --
   unittest then inserts ``tests/`` on sys.path and imports each module
   top-level. That is why README / AGENTS.md / CLAUDE.md all spell the ``-t .``
   form, and this probe fails if the documented form ever stops being covered.
2. Both forms collect the same number of tests, so naming ``-t .`` cannot be
   silently shrinking coverage.

Writes a throwaway test into ``tests/`` for the duration of the run and always
removes it. Run from the repo root.
"""

import os
import re
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
TESTS = REPO / "tests"
MARKER = TESTS / "_probe_zz_tempdir_marker.py"

REPORT_PAYLOAD = (
    "import tempfile, unittest\n"
    "class Probe(unittest.TestCase):\n"
    "    def test_report(self):\n"
    "        open(__file__ + '.out', 'w', encoding='utf-8').write(tempfile.gettempdir())\n"
)

COUNT_CODE = (
    "import unittest\n"
    "suite = unittest.TestLoader().discover('tests', top_level_dir={top!r})\n"
    "print('COUNT', suite.countTestCases())\n"
)


def _env():
    """A child environment like a fresh shell's, for the temp-dir question.

    This probe imports ``tests`` in its own process to learn the redirect
    target, and that import exports TEMP/TMP/TMPDIR into os.environ. Passing
    that straight to the child would let it inherit the redirect and report
    D:\\tmp for *every* command -- measuring inheritance instead of the thing
    under test, which is whether discovery imports the package. Strip the three
    variables so each child starts from the system default, as a plain shell
    does.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO)
    for name in ("TEMP", "TMP", "TMPDIR", "GA_TEST_TEMP"):
        env.pop(name, None)
    return env


def _discover_tempdir(args, produced):
    subprocess.run(
        [sys.executable, "-m", "unittest", "discover"] + args,
        cwd=str(REPO), capture_output=True, text=True, env=_env(), timeout=180,
    )
    try:
        return Path(produced).read_text(encoding="utf-8").strip()
    except OSError:
        return "<not produced>"


def _collect_count(top):
    proc = subprocess.run(
        [sys.executable, "-c", COUNT_CODE.format(top=top)],
        cwd=str(REPO), capture_output=True, text=True, env=_env(), timeout=300,
    )
    out = (proc.stdout or "") + (proc.stderr or "")
    m = re.search(r"^COUNT (\d+)$", out, re.MULTILINE)
    return int(m.group(1)) if m else None


def main():
    sys.path.insert(0, str(REPO))
    import tempfile

    import tests  # noqa: F401  (runs the redirect)

    expected = tempfile.gettempdir()

    MARKER.write_text(REPORT_PAYLOAD, encoding="utf-8")
    produced = Path(str(MARKER) + ".out")
    try:
        pattern = ["-p", "_probe_zz_tempdir_marker.py"]
        plain = _discover_tempdir(["tests"] + pattern, produced)
        if produced.exists():
            produced.unlink()
        documented = _discover_tempdir(["tests", "-t", "."] + pattern, produced)
    finally:
        for p in (MARKER, produced):
            if p.exists():
                p.unlink()

    count_plain = _collect_count(None)
    count_rooted = _collect_count(".")

    print("redirect target            :", expected)
    print("discover -s tests          :", plain, "(not expected to be covered)")
    print("discover -s tests -t .     :", documented, "<- documented form")
    print("counts                     :", count_plain, "vs", count_rooted)

    documented_ok = documented == expected
    counts_ok = count_plain is not None and count_plain == count_rooted

    print("DOCUMENTED_FORM_COVERED:", documented_ok)
    print("SAME_TEST_COUNT:", counts_ok)
    return 0 if (documented_ok and counts_ok) else 1


if __name__ == "__main__":
    raise SystemExit(main())
