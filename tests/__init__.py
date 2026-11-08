"""Test package.

Redirects the standard-library temp directory to ``D:\\tmp`` so a test run does
not consume the system drive, and exports the same location to child processes.

Why a package-level hook and not per-test edits: the suite creates temporary
trees in well over a hundred places via ``tempfile.TemporaryDirectory()``.
Editing every call site would be a large diff for a policy with one correct
value per machine.

Why D: and not just "somewhere else": GA's own runtime state (``temp/``,
assets, sessions) lives at the checkout root, and on Windows ``os.replace`` /
``Path.rename`` fail across drives with WinError 17. Keeping a test's temp tree
on the same volume as the checkout keeps those moves real renames instead of an
error path only some tests happen to exercise.

Coverage: importing this package runs the redirect, so ``python -m unittest
tests.foo`` is covered. Discovery with ``-t .`` also imports it, because the
top-level dir then makes modules import as ``tests.foo``. Plain
``python -m unittest discover -s tests`` does *not* import this file -- unittest
inserts ``tests/`` on sys.path and imports modules top-level -- so the docs name
the ``-t .`` form. Both forms collect the same number of tests (verified).

Set ``GA_TEST_TEMP`` to override, and nothing happens outside Windows unless
that variable is set: a literal ``D:`` path on POSIX would create a bogus
relative directory instead of using a drive.

Nothing here is load-bearing for correctness. If the directory cannot be
created or written, the standard behaviour is kept and tests use the system
temp as before.
"""

import os
import tempfile
from pathlib import Path


def _candidate():
    """The directory to redirect to, or ``None`` to leave the default alone."""
    override = os.environ.get("GA_TEST_TEMP")
    if override:
        return Path(override)
    # "D:/tmp" is only meaningful where drive letters exist. On POSIX it would
    # be a relative path named "D:", so require an explicit override there.
    if os.name == "nt":
        return Path("D:/tmp")
    return None


def _usable(path):
    """Whether ``path`` can hold a temporary tree right now."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        probe = path / ".ga-temp-probe"
        probe.write_bytes(b"")
        probe.unlink()
        return True
    except OSError:
        return False


def _redirect_temp():
    """Point tempfile, and every child process, at the preferred directory."""
    candidate = _candidate()
    if candidate is None or not _usable(candidate):
        return
    resolved = str(candidate)
    # Export for child processes first: a subagent or workflow child inherits
    # the environment, not this interpreter's module state.
    os.environ["TMPDIR"] = resolved
    os.environ["TEMP"] = resolved
    os.environ["TMP"] = resolved
    tempfile.tempdir = resolved


_redirect_temp()
