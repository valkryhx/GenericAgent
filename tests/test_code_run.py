import sys
import tempfile
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


def collect_generator_yields(gen):
    chunks = []
    try:
        while True:
            chunks.append(next(gen))
    except StopIteration as stop:
        return chunks, stop.value


class CodeRunTest(unittest.TestCase):
    def test_python_print_finishes_without_timeout(self):
        result = exhaust_generator(code_run('print("test")', "python", timeout=5, cwd=str(REPO_ROOT / "temp")))

        self.assertEqual("success", result["status"])
        self.assertEqual(0, result["exit_code"])
        self.assertIn("test", result["stdout"])
        self.assertNotIn("Timeout Error", result["stdout"])

    def test_status_display_uses_plain_exit_code_without_emoji(self):
        chunks, result = collect_generator_yields(code_run('print("test")', "python", timeout=5, cwd=str(REPO_ROOT / "temp")))
        display = "".join(chunks)

        self.assertEqual("success", result["status"])
        self.assertIn("[Status] Exit Code: 0", display)
        self.assertNotIn("✅", display)
        self.assertNotIn("❌", display)
        self.assertNotIn("⏳", display)

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

    def test_python_temp_script_exists_during_execution_and_removed_after_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            code_cwd = Path(tmp) / "code_cwd"
            code_cwd.mkdir()
            code = """
from pathlib import Path
p = Path(__file__)
print('exists_during=' + str(p.exists()))
print('suffix=' + ''.join(p.suffixes))
"""

            result = exhaust_generator(code_run(code, "python", timeout=5, cwd=tmp, code_cwd=str(code_cwd)))

            self.assertEqual("success", result["status"])
            self.assertIn("exists_during=True", result["stdout"])
            self.assertIn("suffix=.ai.py", result["stdout"])
            self.assertEqual([], list(code_cwd.glob("*.ai.py")))

    def test_python_missing_code_cwd_returns_clear_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing_code_cwd = Path(tmp) / "missing"

            result = exhaust_generator(code_run('print("test")', "python", timeout=5, cwd=tmp, code_cwd=str(missing_code_cwd)))

            self.assertEqual("error", result["status"])
            self.assertIn("code_cwd does not exist", result["msg"])
            self.assertIn(str(missing_code_cwd), result["msg"])

    def test_py_alias_removes_temporary_script_after_success(self):
        with tempfile.TemporaryDirectory() as tmp:
            code_cwd = Path(tmp) / "code_cwd"
            code_cwd.mkdir()

            result = exhaust_generator(code_run('print("alias")', "py", timeout=5, cwd=tmp, code_cwd=str(code_cwd)))

            self.assertEqual("success", result["status"])
            self.assertIn("alias", result["stdout"])
            self.assertEqual([], list(code_cwd.glob("*.ai.py")))


if __name__ == "__main__":
    unittest.main()
