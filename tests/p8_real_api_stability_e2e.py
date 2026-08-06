from __future__ import annotations

import contextlib
import io
import json
import math
import os
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from tests import p8_real_api_e2e as base
from workflow_models import WorkflowRun
from workflow_runtime import WorkflowRuntime
from workflow_scheduler import SchedulerConfig
from workflow_store import WorkflowStore

CONFIG_NAME = os.environ.get("GA_REAL_API_CONFIG", base.CONFIG_NAME)
EXPECTED_PROFILE_NAME = os.environ.get("GA_REAL_API_EXPECTED_NAME", base.EXPECTED_PROFILE_NAME)
EXPECTED_MODEL = os.environ.get("GA_REAL_API_EXPECTED_MODEL", base.EXPECTED_MODEL)
OPT_IN = os.environ.get("GA_RUN_REAL_API_E2E") == "1"
STABILITY_OPT_IN = os.environ.get("GA_RUN_REAL_API_STABILITY") == "1"
DEFAULT_ROUNDS = 3
MAX_ROUNDS = 10

STABILITY_SCRIPT = r'''
phase('P8 Real API Stability Diagnostic');
log('start p8 real api stability diagnostic round ' + String(args.round));
const single = await agent('Reply with one concise safe sentence containing token GA_P8_STABILITY_SINGLE_' + String(args.round) + '. Do not call tools, MCP, or skills.', {label:'stability-single'});
const pair = await parallel([
  () => agent('Reply with one concise safe sentence containing token GA_P8_STABILITY_A_' + String(args.round) + '. Do not call tools, MCP, or skills.', {label:'stability-parallel-a'}),
  () => agent('Reply with one concise safe sentence containing token GA_P8_STABILITY_B_' + String(args.round) + '. Do not call tools, MCP, or skills.', {label:'stability-parallel-b'}),
]);
return {
  marker: 'GA_P8_STABILITY_DONE',
  round: args.round,
  singleLength: String(single.summary || '').length,
  parallelLengths: pair.map(r => String(r.summary || '').length)
};
'''


def sanitize(value: Any) -> Any:
    return base.sanitize(value)


def load_json(path: Path) -> dict:
    return base.load_json(path)


def scan_for_secret_material(root: Path) -> list[dict]:
    return base.scan_for_secret_material(root)


def check_profile(summary: dict) -> bool:
    return base.check_profile(summary)


def parse_rounds(value: str | None = None) -> int:
    raw = value if value is not None else os.environ.get("GA_REAL_API_STABILITY_ROUNDS")
    try:
        rounds = int(str(raw or DEFAULT_ROUNDS).strip())
    except (TypeError, ValueError):
        rounds = DEFAULT_ROUNDS
    return min(MAX_ROUNDS, max(1, rounds))


def percentile(values: list[float], pct: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    rank = math.ceil((max(0.0, min(100.0, float(pct))) / 100.0) * len(ordered))
    index = max(0, min(len(ordered) - 1, rank - 1))
    return round(ordered[index], 2)


def latency_summary(values: list[float]) -> dict:
    if not values:
        return {"count": 0, "min": None, "max": None, "avg": None, "p50": None, "p95": None}
    return {
        "count": len(values),
        "min": round(min(values), 2),
        "max": round(max(values), 2),
        "avg": round(sum(values) / len(values), 2),
        "p50": percentile(values, 50),
        "p95": percentile(values, 95),
    }


def compute_token_usage_totals(jobs: list[dict]) -> dict:
    return {
        "input_tokens": sum(int((job.get("tokenUsage") or {}).get("input_tokens") or 0) for job in jobs),
        "output_tokens": sum(int((job.get("tokenUsage") or {}).get("output_tokens") or 0) for job in jobs),
        "total_tokens": sum(int((job.get("tokenUsage") or {}).get("total_tokens") or 0) for job in jobs),
    }


def summarize_jobs(run: WorkflowRun, artifact_dir: Path) -> list[dict]:
    jobs = []
    for job in run.jobs:
        result_path = artifact_dir / (job.result_ref or f"agents/{job.job_id}/result.json")
        result_data = load_json(result_path) if result_path.exists() else {}
        token_usage = result_data.get("tokenUsage") or {}
        payload = result_data.get("payload") or {}
        transcript_ref = job.metadata.get("transcriptRef") or result_data.get("transcriptRef")
        transcript_path = artifact_dir / transcript_ref if transcript_ref else None
        jobs.append(
            {
                "jobId": job.job_id,
                "status": job.status,
                "label": job.metadata.get("label"),
                "resultExists": result_path.exists(),
                "transcriptExists": bool(transcript_path and transcript_path.exists()),
                "resultJsonOmitsTranscriptEvents": "transcriptEvents" not in result_data,
                "summaryLength": len(str(payload.get("summary") or "")),
                "tokenUsage": {k: token_usage.get(k) for k in sorted(token_usage) if isinstance(token_usage.get(k), int)},
            }
        )
    return jobs


def run_stability_round(root: Path, round_index: int) -> dict:
    store = WorkflowStore(root / f"round_{round_index}")
    run = store.create_run(
        WorkflowRun(
            run_id=f"wf_p8_stability_round_{round_index}",
            session_id=f"p8_real_api_stability_{round_index}",
            script=STABILITY_SCRIPT,
            status="running",
        )
    )
    runner = base.CountingNativeRunner(config_name=CONFIG_NAME, max_tokens=96, enable_tools=False)
    runtime = WorkflowRuntime(
        store=store,
        runner=runner,
        scheduler_config=SchedulerConfig(max_concurrent=2, max_total=4),
        timeout_seconds=240.0,
    )
    start = time.time()
    error = None
    exception_type = None
    outcome = None
    try:
        outcome = runtime.run(run, args={"suite": "p8-real-api-stability", "round": round_index})
    except Exception as exc:
        error = str(exc)
        exception_type = type(exc).__name__
    elapsed = time.time() - start
    loaded = store.load_run(run.run_id)
    artifact_dir = Path(loaded.artifact_dir)
    jobs = summarize_jobs(loaded, artifact_dir)
    event_types = base.event_types(store, loaded.run_id)
    passed = (
        outcome is not None
        and loaded.status == "succeeded"
        and len(loaded.jobs) == 3
        and runner.started_job_ids == ["agent_1", "agent_2", "agent_3"]
        and all(job.get("status") == "succeeded" for job in jobs)
        and all(job.get("resultExists") and job.get("transcriptExists") and job.get("resultJsonOmitsTranscriptEvents") for job in jobs)
    )
    token_usage_totals = compute_token_usage_totals(jobs)
    return {
        "round": round_index,
        "passed": passed,
        "elapsedSeconds": round(elapsed, 2),
        "runId": loaded.run_id,
        "status": loaded.status,
        "startedJobIds": runner.started_job_ids,
        "eventCounts": {etype: event_types.count(etype) for etype in sorted(set(event_types))},
        "jobs": jobs,
        "error": sanitize(error),
        "exceptionType": exception_type,
        "tokenUsageTotals": token_usage_totals,
        "resultShape": sanitize(outcome.result if outcome else None),
        "artifactDir": str(artifact_dir),
    }


def run_round_safely(root: Path, round_index: int) -> dict:
    start = time.time()
    try:
        result = run_stability_round(root, round_index)
        if not isinstance(result, dict):
            return {
                "round": round_index,
                "passed": False,
                "error": f"round returned non-dict: {type(result).__name__}",
                "exceptionType": "InvalidRoundResult",
                "elapsedSeconds": round(time.time() - start, 2),
            }
        result.setdefault("passed", False)
        return sanitize(result)
    except Exception as exc:
        return {
            "round": round_index,
            "passed": False,
            "error": sanitize(str(exc)),
            "exceptionType": type(exc).__name__,
            "elapsedSeconds": round(time.time() - start, 2),
        }


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="ga_p8_real_api_stability_"))
    rounds = parse_rounds()
    summary: dict[str, Any] = {
        "passed": False,
        "skipped": False,
        "diagnosticOnly": True,
        "configName": CONFIG_NAME,
        "expectedProfileName": EXPECTED_PROFILE_NAME,
        "expectedModel": EXPECTED_MODEL,
        "root": str(root),
        "roundsRequested": rounds,
        "rounds": [],
        "secretScan": [],
        "scannerMode": base.SECRET_SCANNER_MODE,
        "error": None,
    }
    if not OPT_IN:
        summary.update({"skipped": True, "reason": "set GA_RUN_REAL_API_E2E=1 to enable real API stability diagnostics"})
        print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
        return 0
    if not STABILITY_OPT_IN:
        summary.update({"skipped": True, "reason": "set GA_RUN_REAL_API_STABILITY=1 to run repeated real API stability diagnostics"})
        print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
        return 0
    try:
        if not check_profile(summary):
            summary["error"] = f"profile mismatch; expected {EXPECTED_PROFILE_NAME} / {EXPECTED_MODEL}"
            return 2
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            summary["rounds"] = [run_round_safely(root, index) for index in range(1, rounds + 1)]
        summary["capturedLogChars"] = len(captured.getvalue())
        summary["secretScan"] = scan_for_secret_material(root)
        passed_rounds = [round_result for round_result in summary["rounds"] if round_result.get("passed")]
        failed_rounds = [round_result for round_result in summary["rounds"] if not round_result.get("passed")]
        latencies = [float(round_result.get("elapsedSeconds") or 0.0) for round_result in summary["rounds"]]
        observed_errors = sorted({str(round_result.get("exceptionType") or round_result.get("error") or "") for round_result in failed_rounds if round_result.get("exceptionType") or round_result.get("error")})
        summary.update(
            {
                "totalRounds": len(summary["rounds"]),
                "passedRounds": len(passed_rounds),
                "failedRounds": len(failed_rounds),
                "latencySeconds": latency_summary(latencies),
                "observedErrorTypes": observed_errors,
            }
        )
        summary["passed"] = bool(summary["rounds"]) and not failed_rounds and not summary["secretScan"]
        return 0 if summary["passed"] else 2
    except Exception as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
        return 1
    finally:
        print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
