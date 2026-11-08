"""退出码语义化测试。"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from genericagent.core.exit_codes import (  # noqa: E402
    ERROR,
    INFORMATIONAL,
    SUCCESS,
    classify_exit_code,
    exit_code_note,
)


def _classify(command, exit_code, *, code_type="bash", posix=True):
    return classify_exit_code(
        code_type=code_type,
        command=command,
        exit_code=exit_code,
        executor_is_posix_shell=posix,
    )


class ExitCodeTest(unittest.TestCase):
    def test_zero_is_always_success(self):
        for command in ("grep x f", "python foo.py", "true"):
            self.assertEqual(_classify(command, 0), SUCCESS)

    def test_grep_no_match_is_informational(self):
        # The motivating case: exit 1 from grep means "no matches", not failure.
        self.assertEqual(_classify("grep -rn TODO src/", 1), INFORMATIONAL)
        self.assertEqual(_classify("rg TODO", 1), INFORMATIONAL)

    def test_diff_and_test_exit_one_are_informational(self):
        self.assertEqual(_classify("diff a.txt b.txt", 1), INFORMATIONAL)
        self.assertEqual(_classify("test -f missing.txt", 1), INFORMATIONAL)
        self.assertEqual(_classify("[ -f missing.txt ]", 1), INFORMATIONAL)

    def test_exit_two_is_always_an_error(self):
        # 2 is a usage error for essentially every tool; do not soften it.
        self.assertEqual(_classify("grep --bogus-flag x f", 2), ERROR)

    def test_unknown_command_exit_one_stays_an_error(self):
        # Conservative: we only soften the commands whose semantics we know.
        self.assertEqual(_classify("mytool --run", 1), ERROR)

    def test_python_scripts_keep_binary_semantics(self):
        # python/powershell define their own exit conventions; exit 1 is an error.
        self.assertEqual(_classify("raise SystemExit(1)", 1, code_type="python"), ERROR)

    def test_non_posix_executor_never_softens(self):
        # On Windows `type=bash` actually runs PowerShell, where `grep` may not
        # even exist. Applying the POSIX convention there would be a guess.
        self.assertEqual(_classify("grep x f", 1, posix=False), ERROR)

    def test_command_name_parsing_handles_prefixes_and_assignments(self):
        self.assertEqual(_classify("sudo grep x f", 1), INFORMATIONAL)
        self.assertEqual(_classify("FOO=1 BAR=2 grep x f", 1), INFORMATIONAL)
        self.assertEqual(_classify("/usr/bin/grep x f", 1), INFORMATIONAL)
        # Only the first command in a pipeline decides, matching shell semantics.
        self.assertEqual(_classify("grep x f | head -5", 1), INFORMATIONAL)

    def test_quoted_and_empty_commands_do_not_crash(self):
        self.assertEqual(_classify("", 1), ERROR)
        self.assertEqual(_classify('grep "unbalanced', 1), INFORMATIONAL)

    def test_note_explains_what_exit_one_means(self):
        note = exit_code_note(
            code_type="bash", command="grep x f", exit_code=1, executor_is_posix_shell=True,
        )
        self.assertIn("no matches", note)
        self.assertIn("not a failure", note)

    def test_note_is_empty_when_nothing_to_explain(self):
        self.assertEqual(
            exit_code_note(code_type="bash", command="grep x f", exit_code=0, executor_is_posix_shell=True),
            "",
        )
        self.assertEqual(
            exit_code_note(code_type="bash", command="mytool", exit_code=1, executor_is_posix_shell=True),
            "",
        )


if __name__ == "__main__":
    unittest.main()
