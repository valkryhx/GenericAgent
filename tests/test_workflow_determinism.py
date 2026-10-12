import unittest

from workflow_determinism import (
    DETERMINISM_ERROR_MESSAGE,
    determinism_error,
    find_nondeterminism,
    has_nondeterminism,
)


class WorkflowDeterminismTest(unittest.TestCase):
    def test_flags_date_now(self):
        self.assertTrue(has_nondeterminism("return Date.now()"))

    def test_flags_math_random(self):
        self.assertTrue(has_nondeterminism("const x = Math.random()"))

    def test_flags_argumentless_new_date(self):
        self.assertTrue(has_nondeterminism("return new Date()"))
        self.assertTrue(has_nondeterminism("return new Date"))

    def test_allows_new_date_with_argument(self):
        self.assertFalse(has_nondeterminism("return new Date(1700000000000)"))
        self.assertFalse(has_nondeterminism("return new Date(args.ts)"))

    def test_ignores_strings_and_comments(self):
        self.assertFalse(has_nondeterminism("const s = 'Date.now()'"))
        self.assertFalse(has_nondeterminism('const s = "Math.random()"'))
        self.assertFalse(has_nondeterminism("// Date.now()\nreturn 1"))
        self.assertFalse(has_nondeterminism("/* Math.random() */ return 1"))

    def test_scans_template_interpolation(self):
        self.assertTrue(has_nondeterminism("return `at ${Date.now()}`"))
        self.assertFalse(has_nondeterminism("return `Date.now()`"))

    def test_matches_claude_code_bypass_surface(self):
        # Same blind spots as Claude Code's acorn-based LKt().
        self.assertFalse(has_nondeterminism('return Date["now"]()'))
        self.assertFalse(has_nondeterminism("const D = Date; return D.now()"))
        self.assertFalse(has_nondeterminism("return globalThis.Date.now()"))

    def test_find_reports_kinds(self):
        found = find_nondeterminism("Date.now(); Math.random(); new Date()")
        self.assertEqual(["Date.now()", "Math.random()", "new Date()"], found)

    def test_determinism_error_message(self):
        self.assertEqual(DETERMINISM_ERROR_MESSAGE, determinism_error("Date.now()"))
        self.assertIsNone(determinism_error("return 1"))

    def test_empty_and_none(self):
        self.assertFalse(has_nondeterminism(""))
        self.assertFalse(has_nondeterminism(None))


if __name__ == "__main__":
    unittest.main()
