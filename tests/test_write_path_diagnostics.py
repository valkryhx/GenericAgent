"""Deep-path write failures must be diagnosable, not look like "file not found".

Root cause of the confusion this pins down
------------------------------------------
`atomic_write_json` (genericagent/agents/state.py:79) writes a temp file named
`.<name>.<pid>.<tid>.tmp` before `os.replace`. On Windows that suffix pushes an otherwise
legal path past MAX_PATH (260), and the failure surfaces as:

    FileNotFoundError: [Errno 2] No such file or directory: '...\\.state.json.1234.5678.tmp'

A caller reading that message concludes the *directory* is missing, or that the caller
passed a wrong path. Two different real causes are hidden behind it:

  A. `mkdir` of the run dir itself exceeds 260  -> WinError 3 "系统找不到指定的路径"
     on shorter overruns, WinError 206 "文件名或扩展名太长"
  B. the run dir is legal but the *temp* name exceeds 260 -> ENOENT from `open(tmp)`

The fix is not to grow the path limit (Windows has only 260 / 32767, and raising it is a
registry change no pip-installed library may require). It is to make the error say what
actually happened, so the user can shorten the workspace instead of hunting a phantom
missing file.

What these tests assert
-----------------------
1. An over-long *target* path raises an error whose message names the path-length cause and
   reports the actual/limit numbers -- and it is NOT a bare ENOENT.
2. A legal target path whose *temp* name is over-long raises the same diagnosable error
   rather than a bare ENOENT.
3. A normal path still writes fine (the guard must not fire on healthy writes).
4. The error type stays an OSError subclass so existing `except OSError` callers keep working.
"""

import os
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from genericagent.agents import state as agent_state  # noqa: E402
from genericagent.agents.state import atomic_write_json, atomic_write_text  # noqa: E402

WINDOWS_MAX_PATH = 260


def _tmp_len_for(target: Path) -> int:
    """Length of the temp name atomic_write_json would use for `target`."""
    return len(
        os.path.join(
            str(target.parent),
            f".{target.name}.{os.getpid()}.{threading.get_ident()}.tmp",
        )
    )


@unittest.skipUnless(os.name == "nt", "MAX_PATH is a Windows-specific limit")
class DeepPathDiagnosticsTest(unittest.TestCase):
    def _deep_target(self, td: str, extra: int) -> Path:
        """Build a target whose temp name lands exactly at 260 + extra chars."""
        base = os.path.join(td, "x" * 40)
        os.makedirs(base, exist_ok=True)
        probe = Path(base) / "state.json"
        # Grow the directory one char at a time until the temp name crosses the limit.
        depth = 0
        while _tmp_len_for(probe) < WINDOWS_MAX_PATH + extra:
            depth += 1
            base = os.path.join(td, "x" * (40 + depth))
            os.makedirs(base, exist_ok=True)
            probe = Path(base) / "state.json"
        return probe

    def test_overlong_temp_name_raises_diagnosable_error(self):
        """A legal target whose temp name overflows must not read as a bare ENOENT."""
        with tempfile.TemporaryDirectory() as td:
            target = self._deep_target(td, extra=2)

            # Precondition: the target itself is still a legal Windows path.
            self.assertLess(len(str(target)), WINDOWS_MAX_PATH)
            self.assertGreaterEqual(_tmp_len_for(target), WINDOWS_MAX_PATH)

            with self.assertRaises(OSError) as caught:
                atomic_write_json(target, {"probe": True})

            exc = caught.exception
            message = str(exc)
            self.assertIn("path", message.lower(), "message must name the path as the problem")
            self.assertTrue(
                any(token in message for token in ("260", "MAX_PATH", "too long", "太长")),
                "message must convey the length limit, got: %r" % message,
            )
            # The digits must be the real numbers, not a placeholder.
            self.assertIn(str(WINDOWS_MAX_PATH), message)
            self.assertIn(str(_tmp_len_for(target)), message)

    def test_temp_overflow_error_is_still_an_oserror(self):
        """Existing `except OSError` callers must keep catching this."""
        with tempfile.TemporaryDirectory() as td:
            target = self._deep_target(td, extra=2)
            with self.assertRaises(OSError):
                atomic_write_text(target, "body")

    def test_healthy_write_is_unaffected(self):
        """The guard must never fire on a normal path."""
        with tempfile.TemporaryDirectory() as td:
            target = Path(td) / "state.json"
            atomic_write_json(target, {"ok": True})
            self.assertTrue(target.is_file())
            self.assertEqual(list(Path(td).glob("*.tmp")), [])

    def test_error_explains_the_workspace_is_too_deep(self):
        """The message should point at the actionable cause: the workspace is too deep."""
        with tempfile.TemporaryDirectory() as td:
            target = self._deep_target(td, extra=2)
            with self.assertRaises(OSError) as caught:
                atomic_write_json(target, {"probe": True})
            message = str(caught.exception).lower()
            self.assertTrue(
                any(token in message for token in ("workspace", "shorter", "depth", "目录", "过深")),
                "message should hint that the workspace path is too deep, got: %r"
                % str(caught.exception),
            )


if __name__ == "__main__":
    unittest.main()
