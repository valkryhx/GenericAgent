from __future__ import annotations

import contextlib
import io
import json
import unittest
from unittest import mock

from tests import p8_real_api_stability_e2e as stability


class P8RealApiStabilityE2ETest(unittest.TestCase):
    def run_main_with_output(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            code = stability.main()
        return code, json.loads(output.getvalue())

    def test_skips_without_real_api_opt_in(self):
        with mock.patch.object(stability, "OPT_IN", False), \
            mock.patch.object(stability, "STABILITY_OPT_IN", True), \
            mock.patch.object(stability, "check_profile") as check_profile, \
            mock.patch.object(stability, "run_stability_round") as run_round:
            code, summary = self.run_main_with_output()

        self.assertEqual(0, code)
        self.assertTrue(summary["skipped"])
        self.assertFalse(summary["passed"])
        self.assertEqual([], summary["rounds"])
        check_profile.assert_not_called()
        run_round.assert_not_called()

    def test_skips_without_stability_opt_in(self):
        with mock.patch.object(stability, "OPT_IN", True), \
            mock.patch.object(stability, "STABILITY_OPT_IN", False), \
            mock.patch.object(stability, "check_profile") as check_profile, \
            mock.patch.object(stability, "run_stability_round") as run_round:
            code, summary = self.run_main_with_output()

        self.assertEqual(0, code)
        self.assertTrue(summary["skipped"])
        self.assertIn("GA_RUN_REAL_API_STABILITY", summary["reason"])
        check_profile.assert_not_called()
        run_round.assert_not_called()

    def test_aggregates_successful_rounds(self):
        rounds = [
            {"round": 1, "passed": True, "elapsedSeconds": 1.0, "status": "succeeded"},
            {"round": 2, "passed": True, "elapsedSeconds": 3.0, "status": "succeeded"},
            {"round": 3, "passed": True, "elapsedSeconds": 2.0, "status": "succeeded"},
        ]
        with mock.patch.object(stability, "OPT_IN", True), \
            mock.patch.object(stability, "STABILITY_OPT_IN", True), \
            mock.patch.object(stability, "parse_rounds", return_value=3), \
            mock.patch.object(stability, "check_profile", return_value=True), \
            mock.patch.object(stability, "run_stability_round", side_effect=rounds), \
            mock.patch.object(stability, "scan_for_secret_material", return_value=[]):
            code, summary = self.run_main_with_output()

        self.assertEqual(0, code)
        self.assertTrue(summary["passed"])
        self.assertEqual("high-confidence-only", summary["scannerMode"])
        self.assertEqual(3, summary["totalRounds"])
        self.assertEqual(3, summary["passedRounds"])
        self.assertEqual(0, summary["failedRounds"])
        self.assertEqual(1.0, summary["latencySeconds"]["min"])
        self.assertEqual(3.0, summary["latencySeconds"]["max"])
        self.assertEqual([], summary["secretScan"])

    def test_failed_round_is_reported_and_nonzero_exit(self):
        rounds = [
            {"round": 1, "passed": True, "elapsedSeconds": 1.0, "status": "succeeded"},
            {"round": 2, "passed": False, "elapsedSeconds": 2.0, "status": "failed", "exceptionType": "RuntimeError", "error": "redacted failure"},
        ]
        with mock.patch.object(stability, "OPT_IN", True), \
            mock.patch.object(stability, "STABILITY_OPT_IN", True), \
            mock.patch.object(stability, "parse_rounds", return_value=2), \
            mock.patch.object(stability, "check_profile", return_value=True), \
            mock.patch.object(stability, "run_stability_round", side_effect=rounds), \
            mock.patch.object(stability, "scan_for_secret_material", return_value=[]):
            code, summary = self.run_main_with_output()

        self.assertEqual(2, code)
        self.assertFalse(summary["passed"])
        self.assertEqual(1, summary["passedRounds"])
        self.assertEqual(1, summary["failedRounds"])
        self.assertEqual(["RuntimeError"], summary["observedErrorTypes"])

    def test_secret_scan_failure_is_nonzero(self):
        with mock.patch.object(stability, "OPT_IN", True), \
            mock.patch.object(stability, "STABILITY_OPT_IN", True), \
            mock.patch.object(stability, "parse_rounds", return_value=1), \
            mock.patch.object(stability, "check_profile", return_value=True), \
            mock.patch.object(stability, "run_stability_round", return_value={"round": 1, "passed": True, "elapsedSeconds": 1.0}), \
            mock.patch.object(stability, "scan_for_secret_material", return_value=[{"file": "x", "pattern": "token"}]):
            code, summary = self.run_main_with_output()

        self.assertEqual(2, code)
        self.assertFalse(summary["passed"])
        self.assertEqual([{"file": "x", "pattern": "token"}], summary["secretScan"])

    def test_latency_summary_uses_nearest_rank_percentiles(self):
        summary = stability.latency_summary([20.19, 17.16, 18.78])

        self.assertEqual(17.16, summary["min"])
        self.assertEqual(20.19, summary["max"])
        self.assertEqual(18.71, summary["avg"])
        self.assertEqual(18.78, summary["p50"])
        self.assertEqual(20.19, summary["p95"])
        self.assertIsNone(stability.percentile([], 95))

    def test_token_usage_totals_sum_each_field_once(self):
        jobs = [
            {"tokenUsage": {"input_tokens": 10, "output_tokens": 2, "total_tokens": 12}},
            {"tokenUsage": {"input_tokens": 20, "output_tokens": 3, "total_tokens": 23}},
        ]

        token_usage_totals = stability.compute_token_usage_totals(jobs)

        self.assertEqual({"input_tokens": 30, "output_tokens": 5, "total_tokens": 35}, token_usage_totals)

    def test_run_stability_round_disables_tools_for_real_api_child_agents(self):
        captured = {}

        class FakeRunner:
            started_job_ids = ["agent_1", "agent_2", "agent_3"]

            def __init__(self, **kwargs):
                captured.update(kwargs)

        class FakeRuntime:
            def __init__(self, **_kwargs):
                pass

            def run(self, _run, args):
                return mock.Mock(result={"marker": "GA_P8_STABILITY_DONE", "round": args["round"]})

        fake_loaded = mock.Mock()
        fake_loaded.run_id = "wf_p8_stability_round_1"
        fake_loaded.status = "succeeded"
        fake_loaded.artifact_dir = "artifact-dir"
        fake_loaded.jobs = [mock.Mock(), mock.Mock(), mock.Mock()]
        fake_store = mock.Mock()
        fake_store.create_run.return_value = mock.Mock(run_id="wf_p8_stability_round_1")
        fake_store.load_run.return_value = fake_loaded

        with mock.patch.object(stability.base, "CountingNativeRunner", FakeRunner), \
            mock.patch.object(stability, "WorkflowRuntime", FakeRuntime), \
            mock.patch.object(stability, "WorkflowStore", return_value=fake_store), \
            mock.patch.object(stability, "summarize_jobs", return_value=[
                {"status": "succeeded", "resultExists": True, "transcriptExists": True, "resultJsonOmitsTranscriptEvents": True, "tokenUsage": {}},
                {"status": "succeeded", "resultExists": True, "transcriptExists": True, "resultJsonOmitsTranscriptEvents": True, "tokenUsage": {}},
                {"status": "succeeded", "resultExists": True, "transcriptExists": True, "resultJsonOmitsTranscriptEvents": True, "tokenUsage": {}},
            ]), \
            mock.patch.object(stability.base, "event_types", return_value=[]):
            result = stability.run_stability_round(stability.REPO, 1)

        self.assertTrue(result["passed"])
        self.assertFalse(captured["enable_tools"])

    def test_run_stability_round_reports_token_usage_totals_without_legacy_field(self):
        round_result = {
            "round": 1,
            "passed": True,
            "elapsedSeconds": 1.0,
            "tokenUsageTotals": {"input_tokens": 30, "output_tokens": 5, "total_tokens": 35},
        }

        with mock.patch.object(stability, "run_stability_round", return_value=round_result):
            result = stability.run_round_safely(mock.Mock(), 1)

        self.assertEqual({"input_tokens": 30, "output_tokens": 5, "total_tokens": 35}, result["tokenUsageTotals"])
        self.assertNotIn("totalTokenUsageValues", result)

    def test_parse_rounds_clamps_and_defaults(self):
        self.assertEqual(1, stability.parse_rounds("0"))
        self.assertEqual(stability.MAX_ROUNDS, stability.parse_rounds("999"))
        self.assertEqual(stability.DEFAULT_ROUNDS, stability.parse_rounds("not-an-int"))


if __name__ == "__main__":
    unittest.main()
