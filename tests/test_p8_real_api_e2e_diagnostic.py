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
        diagnostic = {"passed": False, "diagnosticOnly": True, "selectedTool": "mcp__fetch__fetch"}
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
            mock.patch.object(p8_real_api_e2e, "run_real_mcp_diagnostic_case", return_value=diagnostic), \
            mock.patch.object(p8_real_api_e2e, "scan_for_secret_material", return_value=[]), \
            contextlib.redirect_stdout(output):
            code = p8_real_api_e2e.main()

        summary = json.loads(output.getvalue())
        self.assertEqual(0, code)
        self.assertTrue(summary["passed"])
        self.assertEqual(diagnostic, summary["diagnostics"]["realMcpDiagnostic"])
        self.assertNotIn("realMcpDiagnostic", summary["cases"])

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
            mock.patch.object(p8_real_api_e2e, "run_real_mcp_diagnostic_case") as diagnostic_case, \
            mock.patch.object(p8_real_api_e2e, "scan_for_secret_material", return_value=[]), \
            contextlib.redirect_stdout(output):
            code = p8_real_api_e2e.main()

        summary = json.loads(output.getvalue())
        self.assertEqual(0, code)
        self.assertTrue(summary["passed"])
        self.assertEqual({}, summary["diagnostics"])
        diagnostic_case.assert_not_called()


if __name__ == "__main__":
    unittest.main()
