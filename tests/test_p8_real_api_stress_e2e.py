from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest import mock

from tests import p8_real_api_stress_e2e as stress


class P8RealApiStressE2ETest(unittest.TestCase):
    def run_main_with_output(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = stress.main()
        return code, json.loads(output.getvalue())

    def test_skips_without_real_api_opt_in(self):
        with mock.patch.object(stress, "OPT_IN", False), \
            mock.patch.object(stress, "STRESS_OPT_IN", True), \
            mock.patch.object(stress, "check_profile") as check_profile, \
            mock.patch.object(stress, "run_stress_round") as run_round:
            code, summary = self.run_main_with_output()
        self.assertEqual(0, code)
        self.assertTrue(summary["skipped"])
        self.assertIn("GA_RUN_REAL_API_E2E", summary["reason"])
        check_profile.assert_not_called()
        run_round.assert_not_called()

    def test_skips_without_stress_opt_in(self):
        with mock.patch.object(stress, "OPT_IN", True), \
            mock.patch.object(stress, "STRESS_OPT_IN", False), \
            mock.patch.object(stress, "check_profile") as check_profile, \
            mock.patch.object(stress, "run_stress_round") as run_round:
            code, summary = self.run_main_with_output()
        self.assertEqual(0, code)
        self.assertTrue(summary["skipped"])
        self.assertIn("GA_RUN_REAL_API_STRESS", summary["reason"])
        check_profile.assert_not_called()
        run_round.assert_not_called()

    def test_aggregates_successful_rounds_without_rate_limit(self):
        rounds = [
            {"round": 1, "passed": True, "elapsedSeconds": 3.0, "status": "succeeded", "rateLimitDetected": False},
            {"round": 2, "passed": True, "elapsedSeconds": 4.0, "status": "succeeded", "rateLimitDetected": False},
        ]
        with mock.patch.object(stress, "OPT_IN", True), \
            mock.patch.object(stress, "STRESS_OPT_IN", True), \
            mock.patch.object(stress, "parse_rounds", return_value=2), \
            mock.patch.object(stress, "parse_fanout", return_value=8), \
            mock.patch.object(stress, "check_profile", return_value=True), \
            mock.patch.object(stress, "run_stress_round", side_effect=rounds), \
            mock.patch.object(stress, "scan_for_secret_material", return_value=[]):
            code, summary = self.run_main_with_output()
        self.assertEqual(0, code)
        self.assertTrue(summary["passed"])
        self.assertEqual(2, summary["contractPassedRounds"])
        self.assertEqual(0, summary["contractFailedRounds"])
        self.assertEqual(2, summary["cleanSuccessRounds"])
        self.assertFalse(summary["rateLimitDetected"])
        self.assertEqual(0, summary["rateLimitRoundCount"])
        self.assertEqual([], summary["rateLimitRounds"])

    def test_aggregates_rate_limit_round_as_passed_diagnostic(self):
        rounds = [{"round": 1, "passed": True, "elapsedSeconds": 2.0, "status": "failed", "rateLimitDetected": True, "error": "HTTP 429 Too Many Requests"}]
        with mock.patch.object(stress, "OPT_IN", True), \
            mock.patch.object(stress, "STRESS_OPT_IN", True), \
            mock.patch.object(stress, "parse_rounds", return_value=1), \
            mock.patch.object(stress, "parse_fanout", return_value=8), \
            mock.patch.object(stress, "check_profile", return_value=True), \
            mock.patch.object(stress, "run_stress_round", side_effect=rounds), \
            mock.patch.object(stress, "scan_for_secret_material", return_value=[]):
            code, summary = self.run_main_with_output()
        self.assertEqual(0, code)
        self.assertTrue(summary["passed"])
        self.assertEqual(1, summary["contractPassedRounds"])
        self.assertEqual(0, summary["contractFailedRounds"])
        self.assertEqual(0, summary["cleanSuccessRounds"])
        self.assertTrue(summary["rateLimitDetected"])
        self.assertEqual(1, summary["rateLimitRoundCount"])
        self.assertEqual([1], summary["rateLimitRounds"])
        self.assertEqual(["HTTP 429 Too Many Requests"], summary["observedErrorTypes"])

    def test_failed_round_is_nonzero_exit(self):
        rounds = [{"round": 1, "passed": False, "elapsedSeconds": 2.0, "exceptionType": "RuntimeError", "error": "boom"}]
        with mock.patch.object(stress, "OPT_IN", True), \
            mock.patch.object(stress, "STRESS_OPT_IN", True), \
            mock.patch.object(stress, "parse_rounds", return_value=1), \
            mock.patch.object(stress, "parse_fanout", return_value=8), \
            mock.patch.object(stress, "check_profile", return_value=True), \
            mock.patch.object(stress, "run_stress_round", side_effect=rounds), \
            mock.patch.object(stress, "scan_for_secret_material", return_value=[]):
            code, summary = self.run_main_with_output()
        self.assertEqual(2, code)
        self.assertFalse(summary["passed"])
        self.assertEqual(1, summary["contractFailedRounds"])

    def test_secret_scan_failure_is_nonzero_exit(self):
        with mock.patch.object(stress, "OPT_IN", True), \
            mock.patch.object(stress, "STRESS_OPT_IN", True), \
            mock.patch.object(stress, "parse_rounds", return_value=1), \
            mock.patch.object(stress, "parse_fanout", return_value=8), \
            mock.patch.object(stress, "check_profile", return_value=True), \
            mock.patch.object(stress, "run_stress_round", return_value={"round": 1, "passed": True, "elapsedSeconds": 1.0}), \
            mock.patch.object(stress, "scan_for_secret_material", return_value=[{"file": "x", "pattern": "token"}]):
            code, summary = self.run_main_with_output()
        self.assertEqual(2, code)
        self.assertFalse(summary["passed"])
        self.assertEqual([{"file": "x", "pattern": "token"}], summary["secretScan"])

    def test_parse_fanout_and_rounds_clamp(self):
        self.assertEqual(1, stress.parse_fanout("0"))
        self.assertEqual(stress.MAX_FANOUT, stress.parse_fanout("999"))
        self.assertEqual(stress.DEFAULT_FANOUT, stress.parse_fanout("bad"))
        self.assertEqual(1, stress.parse_rounds("0"))
        self.assertEqual(stress.MAX_ROUNDS, stress.parse_rounds("999"))

    def test_rate_limit_text_detection(self):
        self.assertTrue(stress.is_rate_limit_text("HTTP 429 Too Many Requests"))
        self.assertTrue(stress.is_rate_limit_text("provider rate limit exceeded"))
        self.assertFalse(stress.is_rate_limit_text("ordinary failure"))

    def test_run_stress_round_disables_tools_for_real_api_child_agents(self):
        captured = {}

        class FakeRunner:
            started_job_ids = ["agent_1", "agent_2"]

            def __init__(self, **kwargs):
                captured.update(kwargs)

        class FakeRuntime:
            def __init__(self, **_kwargs):
                pass

            def run(self, _run, args):
                return mock.Mock(result={"marker": "GA_P8_STRESS_DONE", "fanout": args["fanout"]})

        fake_loaded = mock.Mock()
        fake_loaded.run_id = "wf_p8_stress_round_1"
        fake_loaded.status = "succeeded"
        fake_loaded.artifact_dir = "artifact-dir"
        fake_loaded.jobs = [mock.Mock(), mock.Mock()]
        fake_store = mock.Mock()
        fake_store.create_run.return_value = mock.Mock(run_id="wf_p8_stress_round_1")
        fake_store.load_run.return_value = fake_loaded

        with mock.patch.object(stress.base, "CountingNativeRunner", FakeRunner), \
            mock.patch.object(stress, "WorkflowRuntime", FakeRuntime), \
            mock.patch.object(stress, "WorkflowStore", return_value=fake_store), \
            mock.patch.object(stress, "summarize_jobs", return_value=[
                {"status": "succeeded", "resultExists": True, "transcriptExists": True, "resultJsonOmitsTranscriptEvents": True, "tokenUsage": {}},
                {"status": "succeeded", "resultExists": True, "transcriptExists": True, "resultJsonOmitsTranscriptEvents": True, "tokenUsage": {}},
            ]), \
            mock.patch.object(stress.base, "event_types", return_value=[]):
            result = stress.run_stress_round(stress.REPO, 1, 2)

        self.assertTrue(result["passed"])
        self.assertFalse(captured["enable_tools"])


if __name__ == "__main__":
    unittest.main()
