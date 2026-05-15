import sys
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from ga import code_run  # noqa: E402
import ga  # noqa: E402


def exhaust_generator(gen):
    try:
        while True:
            next(gen)
    except StopIteration as stop:
        return stop.value


class CodeRunTest(unittest.TestCase):
    def test_python_print_finishes_without_timeout(self):
        result = exhaust_generator(code_run('print("test")', "python", timeout=5, cwd=str(REPO_ROOT / "temp")))

        self.assertEqual("success", result["status"])
        self.assertEqual(0, result["exit_code"])
        self.assertIn("test", result["stdout"])
        self.assertNotIn("Timeout Error", result["stdout"])

    def test_subprocess_stdin_is_devnull_for_pipe_frontends(self):
        popen_kwargs = {}

        class EmptyStdout:
            def readline(self):
                return b""

            def close(self):
                pass

        class Proc:
            stdout = EmptyStdout()

            def poll(self):
                return 0

            def kill(self):
                pass

        def fake_popen(_cmd, **kwargs):
            popen_kwargs.update(kwargs)
            return Proc()

        with patch.object(ga.subprocess, "Popen", side_effect=fake_popen):
            result = exhaust_generator(code_run('print("test")', "python", timeout=5, cwd=str(REPO_ROOT / "temp")))

        self.assertEqual("success", result["status"])
        self.assertIs(ga.subprocess.DEVNULL, popen_kwargs.get("stdin"))


if __name__ == "__main__":
    unittest.main()
