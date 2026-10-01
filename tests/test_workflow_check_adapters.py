import tempfile
import unittest
from pathlib import Path

from workflow_check_adapters import run_check


class WorkflowCheckAdapterTest(unittest.TestCase):
    def test_python_unittest_adapter_records_exit_code_and_output(self):
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "test_adapter.py").write_text(
                """import unittest
class AdapterTest(unittest.TestCase):
 def test_output(self): print('adapter-ok')
""",
                encoding="utf-8",
            )
            result = run_check(
                {
                    "id": "tests",
                    "kind": "command",
                    "adapter": "python_unittest",
                    "required": True,
                    "command": ["python", "-m", "unittest", "discover", "-s", "."],
                },
                workspace=Path(td),
                timeout_s=5,
            )

        self.assertEqual(result["checkId"], "tests")
        self.assertEqual(result["evidence"]["exitCode"], 0)
        self.assertEqual(result["status"], "passed")
        self.assertIn("adapter-ok", result["evidence"]["stdout"])

    def test_command_adapter_rejects_shell_string(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(ValueError, "argv list"):
                run_check(
                    {"id": "bad", "kind": "command", "command": "cd .. && del file"},
                    workspace=Path(td),
                    timeout_s=5,
                )

    def test_command_adapter_rejects_unapproved_executable(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(ValueError, "not allowlisted"):
                run_check(
                    {"id": "bad", "kind": "command", "command": ["curl", "https://example.invalid"]},
                    workspace=Path(td),
                    timeout_s=5,
                )

    def test_command_adapter_rejects_inline_python_execution(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaisesRegex(ValueError, "not allowlisted"):
                run_check(
                    {"id": "bad", "kind": "command", "command": ["python", "-c", "print('unsafe')"]},
                    workspace=Path(td),
                    timeout_s=5,
                )

    def test_schema_adapter_requires_evidence_fields(self):
        result = run_check(
            {
                "id": "schema",
                "kind": "schema",
                "schemaRef": "verification_result",
                "required": True,
                "evidence": {"verificationPassed": True, "checks": [], "blockingIssues": []},
            },
            workspace=Path.cwd(),
            timeout_s=5,
        )

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["evidence"]["schemaRef"], "verification_result")

    def test_artifact_adapter_requires_existing_file(self):
        with tempfile.TemporaryDirectory() as td:
            artifact = Path(td) / "report.md"
            artifact.write_text("report", encoding="utf-8")
            result = run_check(
                {"id": "report", "kind": "artifact", "path": "report.md", "required": True},
                workspace=Path(td),
                timeout_s=5,
            )

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["evidence"]["path"], "report.md")


if __name__ == "__main__":
    unittest.main()
