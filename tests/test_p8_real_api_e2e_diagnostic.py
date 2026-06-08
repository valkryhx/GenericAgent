from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest import mock

from tests import p8_real_api_e2e


class P8RealApiE2EDiagnosticTest(unittest.TestCase):
    def test_real_mcp_diagnostic_is_non_gating_and_reported_when_enabled(self):
        case_ok = {"passed": True}
        timeout_diagnostic = {"passed": True, "diagnosticOnly": True, "status": "failed"}
        mcp_diagnostic = {"passed": False, "diagnosticOnly": True, "selectedTool": "mcp__fetch__fetch"}
        output = io.StringIO()

        with mock.patch.object(p8_real_api_e2e, "OPT_IN", True), \
            mock.patch.object(p8_real_api_e2e, "REAL_MCP_OPT_IN", True), \
            mock.patch.object(p8_real_api_e2e, "check_profile", return_value=True), \
            mock.patch.object(p8_real_api_e2e, "run_inherit_permission_smoke_case", return_value=case_ok), \
            mock.patch.object(p8_real_api_e2e, "run_tool_inheritance_real_api_case", return_value=case_ok), \
            mock.patch.object(p8_real_api_e2e, "run_runtime_real_api_case", return_value=case_ok), \
            mock.patch.object(p8_real_api_e2e, "run_failed_source_resume_real_api_case", return_value=case_ok), \
            mock.patch.object(p8_real_api_e2e, "run_killed_source_resume_real_api_case", return_value=case_ok), \
            mock.patch.object(p8_real_api_e2e, "run_bridge_real_api_case", return_value=case_ok), \
            mock.patch.object(p8_real_api_e2e, "run_real_api_timeout_bridge_final_diagnostic_case", return_value=timeout_diagnostic), \
            mock.patch.object(p8_real_api_e2e, "run_real_mcp_diagnostic_case", return_value=mcp_diagnostic), \
            mock.patch.object(p8_real_api_e2e, "scan_for_secret_material", return_value=[]), \
            contextlib.redirect_stdout(output):
            code = p8_real_api_e2e.main()

        summary = json.loads(output.getvalue())
        self.assertEqual(0, code)
        self.assertTrue(summary["passed"])
        self.assertEqual(timeout_diagnostic, summary["diagnostics"]["realApiTimeoutBridgeFinalDiagnostic"])
        self.assertEqual(mcp_diagnostic, summary["diagnostics"]["realMcpDiagnostic"])
        self.assertNotIn("realMcpDiagnostic", summary["cases"])
        self.assertNotIn("realApiTimeoutBridgeFinalDiagnostic", summary["cases"])

    def test_case_level_diagnostic_is_reported_under_case_without_gating_summary(self):
        case_ok = {"passed": True}
        output = io.StringIO()

        with mock.patch.object(p8_real_api_e2e, "OPT_IN", True), \
            mock.patch.object(p8_real_api_e2e, "REAL_MCP_OPT_IN", False), \
            mock.patch.object(p8_real_api_e2e, "check_profile", return_value=True), \
            mock.patch.object(p8_real_api_e2e, "run_inherit_permission_smoke_case", return_value=case_ok), \
            mock.patch.object(p8_real_api_e2e, "run_tool_inheritance_real_api_case", side_effect=FileNotFoundError("tmp123.ai.py missing")), \
            mock.patch.object(p8_real_api_e2e, "run_runtime_real_api_case", return_value=case_ok) as runtime_case, \
            mock.patch.object(p8_real_api_e2e, "run_failed_source_resume_real_api_case", return_value=case_ok), \
            mock.patch.object(p8_real_api_e2e, "run_killed_source_resume_real_api_case", return_value=case_ok), \
            mock.patch.object(p8_real_api_e2e, "run_bridge_real_api_case", return_value=case_ok), \
            mock.patch.object(p8_real_api_e2e, "run_real_api_timeout_bridge_final_diagnostic_case", return_value={"passed": True, "diagnosticOnly": True}), \
            mock.patch.object(p8_real_api_e2e, "scan_for_secret_material", return_value=[]) as secret_scan, \
            contextlib.redirect_stdout(output):
            code = p8_real_api_e2e.main()

        summary = json.loads(output.getvalue())
        self.assertNotEqual(0, code)
        self.assertFalse(summary["passed"])
        self.assertIn("nativeToolCallingFileSkillMcp", summary["cases"])
        failed = summary["cases"]["nativeToolCallingFileSkillMcp"]
        self.assertFalse(failed["passed"])
        self.assertEqual("FileNotFoundError", failed["exceptionType"])
        self.assertIn("tmp123.ai.py missing", failed["error"])
        self.assertIn("runtimeAgentParallelPipelineResume", summary["cases"])
        runtime_case.assert_called_once()
        self.assertIn("realApiTimeoutBridgeFinalDiagnostic", summary["diagnostics"])
        self.assertNotIn("realMcpDiagnostic", summary["diagnostics"])
        self.assertEqual([], summary["secretScan"])
        secret_scan.assert_called_once()

    def test_real_api_suite_skips_without_opt_in(self):
        output = io.StringIO()

        with mock.patch.object(p8_real_api_e2e, "OPT_IN", False), \
            mock.patch.object(p8_real_api_e2e, "check_profile") as check_profile, \
            mock.patch.object(p8_real_api_e2e, "run_inherit_permission_smoke_case") as first_case, \
            contextlib.redirect_stdout(output):
            code = p8_real_api_e2e.main()

        summary = json.loads(output.getvalue())
        self.assertEqual(0, code)
        self.assertTrue(summary["skipped"])
        self.assertFalse(summary["passed"])
        self.assertEqual({}, summary["cases"])
        check_profile.assert_not_called()
        first_case.assert_not_called()

    def test_timeout_bridge_diagnostic_is_non_gating_and_always_reported_when_real_api_opted_in(self):
        case_ok = {"passed": True}
        timeout_diagnostic = {"passed": False, "diagnosticOnly": True, "status": "failed", "errorSeen": True}
        output = io.StringIO()

        with mock.patch.object(p8_real_api_e2e, "OPT_IN", True), \
            mock.patch.object(p8_real_api_e2e, "REAL_MCP_OPT_IN", False), \
            mock.patch.object(p8_real_api_e2e, "check_profile", return_value=True), \
            mock.patch.object(p8_real_api_e2e, "run_inherit_permission_smoke_case", return_value=case_ok), \
            mock.patch.object(p8_real_api_e2e, "run_tool_inheritance_real_api_case", return_value=case_ok), \
            mock.patch.object(p8_real_api_e2e, "run_runtime_real_api_case", return_value=case_ok), \
            mock.patch.object(p8_real_api_e2e, "run_failed_source_resume_real_api_case", return_value=case_ok), \
            mock.patch.object(p8_real_api_e2e, "run_killed_source_resume_real_api_case", return_value=case_ok), \
            mock.patch.object(p8_real_api_e2e, "run_bridge_real_api_case", return_value=case_ok), \
            mock.patch.object(p8_real_api_e2e, "run_real_api_timeout_bridge_final_diagnostic_case", return_value=timeout_diagnostic) as timeout_case, \
            mock.patch.object(p8_real_api_e2e, "scan_for_secret_material", return_value=[]), \
            contextlib.redirect_stdout(output):
            code = p8_real_api_e2e.main()

        summary = json.loads(output.getvalue())
        self.assertEqual(0, code)
        self.assertTrue(summary["passed"])
        self.assertEqual(timeout_diagnostic, summary["diagnostics"]["realApiTimeoutBridgeFinalDiagnostic"])
        self.assertNotIn("realApiTimeoutBridgeFinalDiagnostic", summary["cases"])
        timeout_case.assert_called_once()

    def test_timeout_bridge_diagnostic_exception_is_sanitized_and_non_gating(self):
        case_ok = {"passed": True}
        output = io.StringIO()

        with mock.patch.object(p8_real_api_e2e, "OPT_IN", True), \
            mock.patch.object(p8_real_api_e2e, "REAL_MCP_OPT_IN", False), \
            mock.patch.object(p8_real_api_e2e, "check_profile", return_value=True), \
            mock.patch.object(p8_real_api_e2e, "run_inherit_permission_smoke_case", return_value=case_ok), \
            mock.patch.object(p8_real_api_e2e, "run_tool_inheritance_real_api_case", return_value=case_ok), \
            mock.patch.object(p8_real_api_e2e, "run_runtime_real_api_case", return_value=case_ok), \
            mock.patch.object(p8_real_api_e2e, "run_failed_source_resume_real_api_case", return_value=case_ok), \
            mock.patch.object(p8_real_api_e2e, "run_killed_source_resume_real_api_case", return_value=case_ok), \
            mock.patch.object(p8_real_api_e2e, "run_bridge_real_api_case", return_value=case_ok), \
            mock.patch.object(p8_real_api_e2e, "run_real_api_timeout_bridge_final_diagnostic_case", side_effect=RuntimeError("Bearer secret-token-1234567890")), \
            mock.patch.object(p8_real_api_e2e, "scan_for_secret_material", return_value=[]), \
            contextlib.redirect_stdout(output):
            code = p8_real_api_e2e.main()

        summary = json.loads(output.getvalue())
        self.assertEqual(0, code)
        self.assertTrue(summary["passed"])
        diagnostic = summary["diagnostics"]["realApiTimeoutBridgeFinalDiagnostic"]
        self.assertFalse(diagnostic["passed"])
        self.assertTrue(diagnostic["diagnosticOnly"])
        self.assertEqual("RuntimeError", diagnostic["exceptionType"])
        self.assertIn("[REDACTED]", diagnostic["error"])
        self.assertNotIn("secret-token-1234567890", diagnostic["error"])

    def test_real_mcp_diagnostic_not_run_without_specific_opt_in(self):
        case_ok = {"passed": True}
        output = io.StringIO()

        with mock.patch.object(p8_real_api_e2e, "OPT_IN", True), \
            mock.patch.object(p8_real_api_e2e, "REAL_MCP_OPT_IN", False), \
            mock.patch.object(p8_real_api_e2e, "check_profile", return_value=True), \
            mock.patch.object(p8_real_api_e2e, "run_inherit_permission_smoke_case", return_value=case_ok), \
            mock.patch.object(p8_real_api_e2e, "run_tool_inheritance_real_api_case", return_value=case_ok), \
            mock.patch.object(p8_real_api_e2e, "run_runtime_real_api_case", return_value=case_ok), \
            mock.patch.object(p8_real_api_e2e, "run_failed_source_resume_real_api_case", return_value=case_ok), \
            mock.patch.object(p8_real_api_e2e, "run_killed_source_resume_real_api_case", return_value=case_ok), \
            mock.patch.object(p8_real_api_e2e, "run_bridge_real_api_case", return_value=case_ok), \
            mock.patch.object(p8_real_api_e2e, "run_real_api_timeout_bridge_final_diagnostic_case", return_value={"passed": False, "diagnosticOnly": True}), \
            mock.patch.object(p8_real_api_e2e, "run_real_mcp_diagnostic_case") as diagnostic_case, \
            mock.patch.object(p8_real_api_e2e, "scan_for_secret_material", return_value=[]), \
            contextlib.redirect_stdout(output):
            code = p8_real_api_e2e.main()

        summary = json.loads(output.getvalue())
        self.assertEqual(0, code)
        self.assertTrue(summary["passed"])
        self.assertIn("realApiTimeoutBridgeFinalDiagnostic", summary["diagnostics"])
        self.assertNotIn("realMcpDiagnostic", summary["diagnostics"])
        diagnostic_case.assert_not_called()


if __name__ == "__main__":
    unittest.main()
