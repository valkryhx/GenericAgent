import tempfile
import unittest
import zipfile
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

    def test_artifact_adapter_runs_machine_checks_for_structure_and_sources(self):
        with tempfile.TemporaryDirectory() as td:
            artifact = Path(td) / "report.html"
            artifact.write_text(
                "<html><body><a href='https://docs.python.org/3/library/pathlib.html'>docs</a> "
                "<a href='https://docs.python.org/3/library/functions.html#open'>open</a></body></html>",
                encoding="utf-8",
            )
            structure = run_check(
                {"id": "artifact_structure", "kind": "artifact", "path": "report.html", "required": True},
                workspace=Path(td),
            )
            sources = run_check(
                {"id": "source_count", "kind": "artifact", "path": "report.html", "strict": True, "required": True},
                workspace=Path(td),
            )

        self.assertEqual(structure["status"], "passed")
        self.assertEqual(sources["status"], "passed")
        self.assertEqual(sources["evidence"]["sourceCount"], 2)

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

    def test_artifact_structure_accepts_minimal_docx_zip(self):
        with tempfile.TemporaryDirectory() as td:
            artifact = Path(td) / "report.docx"
            with zipfile.ZipFile(artifact, "w") as package:
                package.writestr("[Content_Types].xml", "<Types/>")
                package.writestr("word/document.xml", "<w:document/>")

            result = run_check(
                {"id": "artifact_structure", "kind": "artifact", "path": "report.docx", "strict": True, "required": True},
                workspace=Path(td),
            )

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["evidence"]["format"], "docx")
        self.assertTrue(result["evidence"]["zipValid"])
        self.assertIn("word/document.xml", result["evidence"]["requiredEntries"])

    def test_artifact_structure_accepts_html_fragment(self):
        with tempfile.TemporaryDirectory() as td:
            artifact = Path(td) / "report.html"
            artifact.write_text("<article><h1>Report</h1></article>", encoding="utf-8")

            result = run_check(
                {"id": "artifact_structure", "kind": "artifact", "path": "report.html", "strict": True, "required": True},
                workspace=Path(td),
            )

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["evidence"]["format"], "html")
        self.assertTrue(result["evidence"]["textReadable"])

    def test_artifact_structure_rejects_corrupt_docx(self):
        with tempfile.TemporaryDirectory() as td:
            artifact = Path(td) / "report.docx"
            artifact.write_bytes(b"not a zip package")

            result = run_check(
                {"id": "artifact_structure", "kind": "artifact", "path": "report.docx", "strict": True, "required": True},
                workspace=Path(td),
            )

        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["evidence"]["zipValid"])

    def test_source_count_accepts_docx_sources_section_without_urls(self):
        with tempfile.TemporaryDirectory() as td:
            artifact = Path(td) / "report.docx"
            document_xml = """<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>Summary</w:t></w:r></w:p>
    <w:p><w:r><w:t>Sources</w:t></w:r></w:p>
    <w:p><w:r><w:t>OpenAI official announcement</w:t></w:r></w:p>
    <w:p><w:r><w:t>Independent arXiv verification</w:t></w:r></w:p>
  </w:body>
</w:document>"""
            with zipfile.ZipFile(artifact, "w") as package:
                package.writestr("[Content_Types].xml", "<Types/>")
                package.writestr("word/document.xml", document_xml)

            result = run_check(
                {"id": "source_count", "kind": "artifact", "path": "report.docx", "minimum": 2, "strict": True, "required": True},
                workspace=Path(td),
            )

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["evidence"]["sourceCount"], 2)
        self.assertEqual(result["evidence"]["sourceCountMode"], "document_entries")

    def test_artifact_structure_defaults_to_observational_evidence(self):
        with tempfile.TemporaryDirectory() as td:
            artifact = Path(td) / "report.docx"
            artifact.write_bytes(b"not inspected by generic contract")

            result = run_check(
                {"id": "artifact_structure", "kind": "artifact", "path": "report.docx", "required": True},
                workspace=Path(td),
            )

        self.assertEqual(result["status"], "passed")
        self.assertEqual(result["evidence"]["validationMode"], "observational")
        self.assertTrue(result["evidence"]["nonEmpty"])
        self.assertNotIn("zipValid", result["evidence"])


if __name__ == "__main__":
    unittest.main()
