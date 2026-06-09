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
STRESS_OPT_IN = os.environ.get("GA_RUN_REAL_API_STRESS") == "1"
DEFAULT_FANOUT = 8
MAX_FANOUT = 32
DEFAULT_ROUNDS = 1
MAX_ROUNDS = 5

STRESS_SCRIPT = r'''
phase('P8 Real API Stress Diagnostic');
log('start p8 real api stress diagnostic round ' + String(args.round) + ' fanout ' + String(args.fanout));
const items = Array.from({length: args.fanout}, (_, index) => index + 1);
const results = await parallel(items.map(index => () => agent(
  'Reply with one concise safe sentence containing marker GA_P8_STRESS_' + String(args.round) + '_' + String(index) + '. Do not call tools, MCP, or skills. Do not read mykey.py, mykey.json, or mcp.json.',
  {label:'stress-' + String(index)}
)));
return {
  marker: 'GA_P8_STRESS_DONE',
  round: args.round,
  fanout: args.fanout,
  resultLengths: results.map(r => String(r.summary || '').length)
};
'''


def sanitize(value: Any) -> Any:
    return base.sanitize(value)


def load_json(path: Path) -> dict:
    return base.load_json(path)


def scan_for_secret_material(root: Path) -> list[dict]:
    return base.scan_for_secret_material(root)


def check_profile(summary: dict) -> bool:
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        from llmcore import reload_mykeys
        cfg = reload_mykeys()[0].get(CONFIG_NAME) or {}
    profile = {
        "configName": CONFIG_NAME,
        "name": cfg.get("name"),
        "model": cfg.get("model"),
        "apiMode": cfg.get("api_mode", "chat_completions"),
        "loadLogChars": len(captured.getvalue()),
    }
    ok = profile["name"] == EXPECTED_PROFILE_NAME and profile["model"] == EXPECTED_MODEL
    summary["profile"] = profile
    summary["profileOk"] = ok
    return ok


def parse_int_env(name: str, default: int, maximum: int, value: str | None = None) -> int:
    raw = value if value is not None else os.environ.get(name)
    try:
        parsed = int(str(raw or default).strip())
    except (TypeError, ValueError):
        parsed = default
    return min(maximum, max(1, parsed))


def parse_rounds(value: str | None = None) -> int:
    return parse_int_env("GA_REAL_API_STRESS_ROUNDS", DEFAULT_ROUNDS, MAX_ROUNDS, value)


def parse_fanout(value: str | None = None) -> int:
    return parse_int_env("GA_REAL_API_STRESS_FANOUT", DEFAULT_FANOUT, MAX_FANOUT, value)


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


def is_rate_limit_text(value: Any) -> bool:
    text = str(value or "").lower()
    return "429" in text or "rate limit" in text or "ratelimit" in text or "too many requests" in text


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
        error = payload.get("error") or result_data.get("error")
        jobs.append(
            {
                "jobId": job.job_id,
                "status": job.status,
                "label": job.metadata.get("label"),
                "resultExists": result_path.exists(),
                "transcriptExists": bool(transcript_path and transcript_path.exists()),
                "resultJsonOmitsTranscriptEvents": "transcriptEvents" not in result_data,
                "summaryLength": len(str(payload.get("summary") or "")),
                "error": sanitize(error),
                "rateLimitDetected": is_rate_limit_text(error),
                "tokenUsage": {k: token_usage.get(k) for k in sorted(token_usage) if isinstance(token_usage.get(k), int)},
            }
        )
    return jobs


def run_stress_round(root: Path, round_index: int, fanout: int) -> dict:
    store = WorkflowStore(root / f"round_{round_index}")
    run = store.create_run(
        WorkflowRun(
            run_id=f"wf_p8_stress_round_{round_index}",
            session_id=f"p8_real_api_stress_{round_index}",
            script=STRESS_SCRIPT,
            status="running",
        )
    )
    runner = base.CountingNativeRunner(config_name=CONFIG_NAME, max_tokens=96, enable_tools=False)
    runtime = WorkflowRuntime(
        store=store,
        runner=runner,
        scheduler_config=SchedulerConfig(max_concurrent=min(fanout, 8), max_total=max(fanout + 2, 4)),
        timeout_seconds=420.0,
    )
    start = time.time()
    error = None
    exception_type = None
    outcome = None
    try:
        outcome = runtime.run(run, args={"suite": "p8-real-api-stress", "round": round_index, "fanout": fanout})
    except Exception as exc:
        error = str(exc)
        exception_type = type(exc).__name__
    elapsed = time.time() - start
    loaded = store.load_run(run.run_id)
    artifact_dir = Path(loaded.artifact_dir)
    jobs = summarize_jobs(loaded, artifact_dir)
    event_types = base.event_types(store, loaded.run_id)
    failed_jobs = [job for job in jobs if job.get("status") == "failed"]
    succeeded_jobs = [job for job in jobs if job.get("status") == "succeeded"]
    rate_limit_detected = is_rate_limit_text(error) or any(job.get("rateLimitDetected") for job in jobs)
    success_ok = outcome is not None and loaded.status == "succeeded" and len(succeeded_jobs) == fanout
    failure_ok = loaded.status == "failed" and bool(failed_jobs) and "agent_failed" in event_types and "workflow_failed" in event_types
    artifact_ok = all(job.get("resultExists") and job.get("transcriptExists") and job.get("resultJsonOmitsTranscriptEvents") for job in jobs)
    return {
        "round": round_index,
        "passed": (success_ok or failure_ok) and artifact_ok,
        "elapsedSeconds": round(elapsed, 2),
        "runId": loaded.run_id,
        "status": loaded.status,
        "fanout": fanout,
        "startedJobIds": runner.started_job_ids,
        "eventCounts": {etype: event_types.count(etype) for etype in sorted(set(event_types))},
        "jobs": jobs,
        "succeededJobCount": len(succeeded_jobs),
        "failedJobCount": len(failed_jobs),
        "rateLimitDetected": rate_limit_detected,
        "error": sanitize(error or loaded.error),
        "exceptionType": exception_type,
        "tokenUsageTotals": compute_token_usage_totals(jobs),
        "resultShape": sanitize(outcome.result if outcome else None),
        "artifactDir": str(artifact_dir),
    }


def run_round_safely(root: Path, round_index: int, fanout: int) -> dict:
    start = time.time()
    try:
        result = run_stress_round(root, round_index, fanout)
        if not isinstance(result, dict):
            return {"round": round_index, "passed": False, "error": f"round returned non-dict: {type(result).__name__}", "exceptionType": "InvalidRoundResult", "elapsedSeconds": round(time.time() - start, 2)}
        result.setdefault("passed", False)
        return sanitize(result)
    except Exception as exc:
        return {"round": round_index, "passed": False, "error": sanitize(str(exc)), "exceptionType": type(exc).__name__, "elapsedSeconds": round(time.time() - start, 2)}


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="ga_p8_real_api_stress_"))
    rounds = parse_rounds()
    fanout = parse_fanout()
    summary: dict[str, Any] = {
        "passed": False,
        "skipped": False,
        "diagnosticOnly": True,
        "configName": CONFIG_NAME,
        "expectedProfileName": EXPECTED_PROFILE_NAME,
        "expectedModel": EXPECTED_MODEL,
        "root": str(root),
        "roundsRequested": rounds,
        "fanout": fanout,
        "rounds": [],
        "secretScan": [],
        "error": None,
    }
    if not OPT_IN:
        summary.update({"skipped": True, "reason": "set GA_RUN_REAL_API_E2E=1 to enable real API stress diagnostics"})
        print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
        return 0
    if not STRESS_OPT_IN:
        summary.update({"skipped": True, "reason": "set GA_RUN_REAL_API_STRESS=1 to run real API stress diagnostics"})
        print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
        return 0
    try:
        if not check_profile(summary):
            summary["error"] = "profile mismatch; expected configured real API profile/model"
            return 2
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            summary["rounds"] = [run_round_safely(root, index, fanout) for index in range(1, rounds + 1)]
        summary["capturedLogChars"] = len(captured.getvalue())
        summary["secretScan"] = scan_for_secret_material(root)
        contract_passed_rounds = [round_result for round_result in summary["rounds"] if round_result.get("passed")]
        contract_failed_rounds = [round_result for round_result in summary["rounds"] if not round_result.get("passed")]
        rate_limit_rounds = [round_result for round_result in summary["rounds"] if round_result.get("rateLimitDetected")]
        clean_success_rounds = [
            round_result
            for round_result in contract_passed_rounds
            if not round_result.get("rateLimitDetected") and str(round_result.get("status") or "") == "succeeded"
        ]
        latencies = [float(round_result.get("elapsedSeconds") or 0.0) for round_result in summary["rounds"]]
        observed_errors = sorted({str(round_result.get("exceptionType") or round_result.get("error") or "") for round_result in summary["rounds"] if round_result.get("exceptionType") or round_result.get("error")})
        summary.update(
            {
                "totalRounds": len(summary["rounds"]),
                "contractPassedRounds": len(contract_passed_rounds),
                "contractFailedRounds": len(contract_failed_rounds),
                "cleanSuccessRounds": len(clean_success_rounds),
                "rateLimitDetected": bool(rate_limit_rounds),
                "rateLimitRoundCount": len(rate_limit_rounds),
                "rateLimitRounds": [round_result.get("round") for round_result in rate_limit_rounds],
                "latencySeconds": latency_summary(latencies),
                "observedErrorTypes": observed_errors,
            }
        )
        summary["passed"] = bool(summary["rounds"]) and not contract_failed_rounds and not summary["secretScan"]
        return 0 if summary["passed"] else 2
    except Exception as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
        return 1
    finally:
        print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
