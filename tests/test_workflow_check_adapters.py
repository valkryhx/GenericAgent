import json
import tempfile
import unittest
import zipfile
from pathlib import Path

from workflow_check_adapters import run_check
from workflow_models import WorkflowJob, WorkflowRun


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


class HostAuthoredEvidenceTest(unittest.TestCase):
    """A verifier cannot be both read-only and the writer of its own evidence.

    Real deepseek run wf_06af574f: the plan named the cross-verifier as the
    writer of evidence/verification_result.json and attached three hard checks,
    while the host had already given that role the non-mutating ``verify``
    profile. The verifier's file_write was denied (verify_profile_no_mutation),
    the file never existed and the whole run failed on missing_artifact even
    though every agent succeeded.

    Step-Code never puts that contradiction in front of the model: its QA agent
    is read-only and *returns* structured evidence, and the host persists it
    (runtime.ts writeEvidence -> journal.appendEvidence). The host must own the
    write for any artifact whose declared writer runs under a non-mutating
    profile.
    """

    def test_host_writes_evidence_for_a_read_only_writer(self):
        from workflow_runtime import write_contract_evidence_if_needed

        with tempfile.TemporaryDirectory() as tmp:
            run = WorkflowRun(
                run_id="wf_evidence", script="",
                session_id="s_evidence",
                metadata={
                    "workspacePath": tmp,
                    "executionContract": {
                        "requiresExecution": True,
                        "artifacts": [
                            {"path": "evidence/verification_result.json", "writer": "Verifier",
                             "requiredChecks": ["artifact_exists", "artifact_readback"]},
                        ],
                    },
                },
            )
            run.jobs = [
                WorkflowJob(
                    job_id="agent_1",
                    prompt="verify",
                    status="succeeded",
                    metadata={
                        "label": "Verifier",
                        "role": "verification",
                        "permissionProfile": "verify",
                        "result": {"verificationPassed": True, "checks": [], "blockingIssues": []},
                    },
                )
            ]

            written = write_contract_evidence_if_needed(run)

            self.assertEqual(["evidence/verification_result.json"], written)
            target = Path(tmp) / "evidence" / "verification_result.json"
            self.assertTrue(target.is_file())
            payload = json.loads(target.read_text(encoding="utf-8"))
            self.assertTrue(payload["verificationPassed"])
            self.assertEqual("Verifier", payload["evidenceAuthor"])

    def test_host_does_not_write_for_a_mutating_writer(self):
        from workflow_runtime import write_contract_evidence_if_needed

        with tempfile.TemporaryDirectory() as tmp:
            run = WorkflowRun(
                run_id="wf_evidence_2", script="",
                session_id="s_evidence_2",
                metadata={
                    "workspacePath": tmp,
                    "executionContract": {
                        "requiresExecution": True,
                        "artifacts": [
                            {"path": "report.html", "writer": "Writer",
                             "requiredChecks": ["artifact_exists"]},
                        ],
                    },
                },
            )
            run.jobs = [
                WorkflowJob(
                    job_id="agent_1", prompt="write", status="succeeded",
                    metadata={"label": "Writer", "role": "synthesis",
                              "permissionProfile": "inherit-current-permissions",
                              "result": {"text": "<html></html>"}},
                )
            ]

            self.assertEqual([], write_contract_evidence_if_needed(run))
            self.assertFalse((Path(tmp) / "report.html").exists())

    def test_host_does_not_overwrite_an_artifact_the_writer_already_produced(self):
        from workflow_runtime import write_contract_evidence_if_needed

        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "evidence" / "verification_result.json"
            target.parent.mkdir(parents=True)
            target.write_text('{"verificationPassed": false}', encoding="utf-8")
            run = WorkflowRun(
                run_id="wf_evidence_3", script="", session_id="s_evidence_3",
                metadata={
                    "workspacePath": tmp,
                    "executionContract": {
                        "requiresExecution": True,
                        "artifacts": [
                            {"path": "evidence/verification_result.json", "writer": "Verifier",
                             "requiredChecks": ["artifact_exists"]},
                        ],
                    },
                },
            )
            run.jobs = [
                WorkflowJob(job_id="agent_1", prompt="verify", status="succeeded",
                            metadata={"label": "Verifier", "role": "verification",
                                      "permissionProfile": "verify",
                                      "result": {"verificationPassed": True}}),
            ]

            self.assertEqual([], write_contract_evidence_if_needed(run))
            self.assertFalse(json.loads(target.read_text(encoding="utf-8"))["verificationPassed"])
