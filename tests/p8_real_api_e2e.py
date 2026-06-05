from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
FRONTENDS = REPO / "frontends"
if str(FRONTENDS) not in sys.path:
    sys.path.insert(0, str(FRONTENDS))

from ink_bridge import GenericAgentBridge
from workflow_child_agent import NativeGPTChildAgentRunner
from workflow_controller import WorkflowController
from workflow_models import WorkflowRun
from workflow_runtime import WorkflowRuntime
from workflow_scheduler import SchedulerConfig
from workflow_store import WorkflowStore

CONFIG_NAME = os.environ.get("GA_REAL_API_CONFIG", "native_oai_config")
EXPECTED_PROFILE_NAME = os.environ.get("GA_REAL_API_EXPECTED_NAME", "gpt-native")
EXPECTED_MODEL = os.environ.get("GA_REAL_API_EXPECTED_MODEL", "gpt-5.5")
OPT_IN = os.environ.get("GA_RUN_REAL_API_E2E") == "1"

SECRET_PATTERNS = [
    ("bearer", re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{12,}", re.IGNORECASE)),
    ("x_api_key", re.compile(r"(x-api-key\s*[:=]\s*)[^\s,;\]}]+", re.IGNORECASE)),
    ("api_key_field", re.compile(r"((?:api[_-]?key|apikey|token|secret|password)\s*[\"']?\s*[:=]\s*[\"']?)[^\"'\s,;\]}]+", re.IGNORECASE)),
    ("sk_token", re.compile(r"sk-[A-Za-z0-9_-]{12,}")),
    ("sk_ant_token", re.compile(r"sk-ant-[A-Za-z0-9_-]{12,}")),
    ("jwt", re.compile(r"eyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}")),
]

RUNTIME_SCRIPT = r'''
phase('P8 Real API Runtime E2E');
log('start p8 real api runtime e2e');
const single = await agent('Reply with one concise sentence containing token GA_P8_SINGLE.', {label:'single-real'});
const pair = await parallel([
  () => agent('Reply with one concise sentence containing token GA_P8_PARALLEL_A.', {label:'parallel-a'}),
  () => agent('Reply with one concise sentence containing token GA_P8_PARALLEL_B.', {label:'parallel-b'}),
]);
const piped = await pipeline(
  ['x', 'y'],
  async item => agent('Reply with one concise sentence containing token GA_P8_STAGE1_' + item.toUpperCase() + '.', {label:'stage1-' + item}),
  async prev => agent('Reply with one concise sentence containing token GA_P8_STAGE2 and mention whether prior text was nonempty: ' + Boolean(prev.summary), {label:'stage2'})
);
return {
  marker: 'GA_P8_RUNTIME_DONE',
  singleLength: String(single.summary || '').length,
  parallelLengths: pair.map(r => String(r.summary || '').length),
  pipelineLengths: piped.map(r => String(r.summary || '').length)
};
'''

BRIDGE_SCRIPT = r'''
phase('P8 Real API Bridge E2E');
log('start p8 real api bridge e2e');
const result = await agent('Reply with one concise sentence containing token GA_P8_BRIDGE.', {label:'bridge-real'});
return { marker: 'GA_P8_BRIDGE_DONE', length: String(result.summary || '').length };
'''

FAILED_SOURCE_SCRIPT = r'''
phase('P8 Failed Source Resume E2E');
log('start p8 failed source resume e2e');
const first = await agent('Reply with one concise sentence containing token GA_P8_FAILED_PREFIX.', {label:'failed-prefix'});
throw new Error('GA_P8_FORCED_SOURCE_FAILURE after prefix length ' + String(first.summary || '').length);
'''

RESUME_AFTER_FAILED_SCRIPT = r'''
phase('P8 Failed Source Resume E2E');
log('resume p8 failed source resume e2e');
const first = await agent('Reply with one concise sentence containing token GA_P8_FAILED_PREFIX.', {label:'failed-prefix'});
const second = await agent('Reply with one concise sentence containing token GA_P8_FAILED_RESUME_FRESH.', {label:'resume-fresh'});
return {
  marker: 'GA_P8_FAILED_RESUME_DONE',
  firstLength: String(first.summary || '').length,
  secondLength: String(second.summary || '').length
};
'''

KILLED_SOURCE_SCRIPT = r'''
phase('P8 Killed Source Resume E2E');
log('start p8 killed source resume e2e');
const first = await agent('Reply with one concise sentence containing token GA_P8_KILLED_PREFIX.', {label:'killed-prefix'});
const second = await agent('Reply with one concise sentence containing token GA_P8_KILLED_SOURCE_SHOULD_BE_CANCELLED.', {label:'killed-cancelled'});
return {
  marker: 'GA_P8_KILLED_SOURCE_UNEXPECTED_DONE',
  firstLength: String(first.summary || '').length,
  secondLength: String(second.summary || '').length
};
'''

RESUME_AFTER_KILLED_SCRIPT = r'''
phase('P8 Killed Source Resume E2E');
log('resume p8 killed source resume e2e');
const first = await agent('Reply with one concise sentence containing token GA_P8_KILLED_PREFIX.', {label:'killed-prefix'});
const second = await agent('Reply with one concise sentence containing token GA_P8_KILLED_RESUME_FRESH.', {label:'killed-resume-fresh'});
return {
  marker: 'GA_P8_KILLED_RESUME_DONE',
  firstLength: String(first.summary || '').length,
  secondLength: String(second.summary || '').length
};
'''


class CountingNativeRunner(NativeGPTChildAgentRunner):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.started_job_ids: list[str] = []

    def start(self, job) -> None:
        self.started_job_ids.append(job.job_id)
        return super().start(job)


class GateOnSecondStartRunner(CountingNativeRunner):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.second_started = threading.Event()
        self.cancelled_job_ids: list[str] = []

    def start(self, job) -> None:
        if job.job_id == "agent_2":
            self.started_job_ids.append(job.job_id)
            self.second_started.set()
            return
        return super().start(job)

    def cancel(self, job) -> None:
        self.cancelled_job_ids.append(job.job_id)
        return None


class MinimalAgent:
    def __init__(self):
        self.inc_out = False
        self.verbose = True
        self.is_running = False
        self.session_id = "p8_real_api_bridge_session"

    def run(self):
        return None

    def abort(self):
        return None


def sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): sanitize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(v) for v in value]
    if isinstance(value, tuple):
        return [sanitize(v) for v in value]
    if not isinstance(value, str):
        return value
    text = value
    for _name, pattern in SECRET_PATTERNS:
        def repl(match):
            if match.lastindex:
                return match.group(1) + "[REDACTED]"
            return "[REDACTED]"
        text = pattern.sub(repl, text)
    return text


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]


def scan_for_secret_material(root: Path) -> list[dict]:
    hits = []
    if not root.exists():
        return hits
    for path in root.rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        for name, pattern in SECRET_PATTERNS:
            if pattern.search(text):
                hits.append({"file": str(path.relative_to(root)), "pattern": name})
    return hits


def event_types(store: WorkflowStore, run_id: str) -> list[str]:
    return [event.event_type for event in store.replay_events(run_id)]


def assert_parallel_started_before_first_completion(events: list[dict], job_ids: set[str]) -> bool:
    first_target_completion = None
    started_before = set()
    for index, event in enumerate(events):
        etype = event.get("type")
        job_id = event.get("jobId")
        if etype == "agent_completed" and job_id in job_ids and first_target_completion is None:
            first_target_completion = index
        if etype == "agent_started" and job_id in job_ids and first_target_completion is None:
            started_before.add(job_id)
    return job_ids.issubset(started_before)


def summarize_jobs(run: WorkflowRun, artifact_dir: Path) -> list[dict]:
    jobs = []
    for job in run.jobs:
        result_path = artifact_dir / (job.result_ref or f"agents/{job.job_id}/result.json")
        result_data = load_json(result_path) if result_path.exists() else {}
        transcript_ref = job.metadata.get("transcriptRef") or result_data.get("transcriptRef")
        transcript_path = artifact_dir / transcript_ref if transcript_ref else None
        token_usage = result_data.get("tokenUsage") or {}
        jobs.append(
            {
                "jobId": job.job_id,
                "status": job.status,
                "label": job.metadata.get("label"),
                "resultExists": result_path.exists(),
                "transcriptExists": bool(transcript_path and transcript_path.exists()),
                "resultJsonOmitsTranscriptEvents": "transcriptEvents" not in result_data,
                "summaryLength": len(str((result_data.get("payload") or {}).get("summary") or "")),
                "tokenUsage": {k: token_usage.get(k) for k in sorted(token_usage) if isinstance(token_usage.get(k), int)},
                "cachedFromRunId": job.metadata.get("cachedFromRunId"),
                "cachedFromJobId": job.metadata.get("cachedFromJobId"),
            }
        )
    return jobs


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


def run_runtime_real_api_case(root: Path) -> dict:
    store = WorkflowStore(root / "runtime")
    run = store.create_run(WorkflowRun(run_id="wf_p8_runtime_source", session_id="p8_real_api_runtime", script=RUNTIME_SCRIPT, status="running"))
    runner = CountingNativeRunner(config_name=CONFIG_NAME, max_tokens=96)
    runtime = WorkflowRuntime(
        store=store,
        runner=runner,
        scheduler_config=SchedulerConfig(max_concurrent=2, max_total=12),
        timeout_seconds=420.0,
    )
    start = time.time()
    outcome = runtime.run(run, args={"suite": "p8-real-api", "case": "runtime"})
    elapsed = time.time() - start
    source = store.load_run(run.run_id)
    artifact_dir = Path(source.artifact_dir)
    journal = read_jsonl(artifact_dir / "journal.jsonl")
    types = [event.get("type") for event in journal]
    jobs = summarize_jobs(source, artifact_dir)

    resumed = store.create_run(WorkflowRun(run_id="wf_p8_runtime_resumed", session_id="p8_real_api_runtime", script=RUNTIME_SCRIPT, status="running"))
    resume_runner = CountingNativeRunner(config_name=CONFIG_NAME, max_tokens=96)
    resume_runtime = WorkflowRuntime(
        store=store,
        runner=resume_runner,
        scheduler_config=SchedulerConfig(max_concurrent=2, max_total=12),
        timeout_seconds=120.0,
    )
    resume_outcome = resume_runtime.run(resumed, args={"suite": "p8-real-api", "case": "runtime"}, resume_from_run_id=source.run_id)
    resumed_loaded = store.load_run(resumed.run_id)
    resumed_artifact_dir = Path(resumed_loaded.artifact_dir)
    resumed_types = event_types(store, resumed.run_id)
    resumed_jobs = summarize_jobs(resumed_loaded, resumed_artifact_dir)

    runtime_ok = (
        outcome.run.status == "succeeded"
        and len(source.jobs) == 7
        and len(runner.started_job_ids) == 7
        and all(job["status"] == "succeeded" for job in jobs)
        and all(job["resultExists"] and job["transcriptExists"] and job["resultJsonOmitsTranscriptEvents"] and job["summaryLength"] > 0 for job in jobs)
        and {"workflow_phase", "workflow_log", "agent_registered", "agent_started", "agent_completed"}.issubset(set(types))
        and assert_parallel_started_before_first_completion(journal, {"agent_2", "agent_3"})
    )
    resume_ok = (
        resume_outcome.run.status == "succeeded"
        and resume_runner.started_job_ids == []
        and len(resumed_loaded.jobs) == 7
        and all(job["status"] == "cached" for job in resumed_jobs)
        and resumed_types.count("agent_cached") == 7
        and "agent_started" not in resumed_types
        and resume_outcome.result == outcome.result
        and all(job["resultExists"] for job in resumed_jobs)
    )
    return {
        "passed": runtime_ok and resume_ok,
        "elapsedSeconds": round(elapsed, 2),
        "sourceRunId": source.run_id,
        "sourceStatus": source.status,
        "sourceStartedJobIds": runner.started_job_ids,
        "sourceEventCounts": {etype: types.count(etype) for etype in sorted(set(types))},
        "sourceJobs": jobs,
        "parallelStartedBeforeFirstCompletion": assert_parallel_started_before_first_completion(journal, {"agent_2", "agent_3"}),
        "runtimeResultShape": sanitize(outcome.result),
        "resumedRunId": resumed_loaded.run_id,
        "resumedStatus": resumed_loaded.status,
        "resumedStartedJobIds": resume_runner.started_job_ids,
        "resumedEventCounts": {etype: resumed_types.count(etype) for etype in sorted(set(resumed_types))},
        "resumedJobs": resumed_jobs,
        "resumeResultEqualsSource": resume_outcome.result == outcome.result,
        "artifactDir": str(artifact_dir),
        "resumedArtifactDir": str(resumed_artifact_dir),
    }


def run_failed_source_resume_real_api_case(root: Path) -> dict:
    store = WorkflowStore(root / "failed_resume")
    source = store.create_run(WorkflowRun(run_id="wf_p8_failed_source", session_id="p8_real_api_failed_resume", script=FAILED_SOURCE_SCRIPT, status="running"))
    source_runner = CountingNativeRunner(config_name=CONFIG_NAME, max_tokens=96)
    source_runtime = WorkflowRuntime(
        store=store,
        runner=source_runner,
        scheduler_config=SchedulerConfig(max_concurrent=1, max_total=4),
        timeout_seconds=240.0,
    )
    start = time.time()
    source_error = None
    try:
        source_runtime.run(source, args={"suite": "p8-real-api", "case": "failed-resume"})
    except Exception as exc:
        source_error = f"{type(exc).__name__}: {exc}"
    elapsed = time.time() - start
    source_loaded = store.load_run(source.run_id)
    source_artifact_dir = Path(source_loaded.artifact_dir)
    source_jobs = summarize_jobs(source_loaded, source_artifact_dir)
    source_types = event_types(store, source.run_id)

    resumed = store.create_run(WorkflowRun(run_id="wf_p8_failed_resumed", session_id="p8_real_api_failed_resume", script=RESUME_AFTER_FAILED_SCRIPT, status="running"))
    resume_runner = CountingNativeRunner(config_name=CONFIG_NAME, max_tokens=96)
    resume_runtime = WorkflowRuntime(
        store=store,
        runner=resume_runner,
        scheduler_config=SchedulerConfig(max_concurrent=1, max_total=4),
        timeout_seconds=240.0,
    )
    resume_outcome = resume_runtime.run(resumed, args={"suite": "p8-real-api", "case": "failed-resume"}, resume_from_run_id=source_loaded.run_id)
    resumed_loaded = store.load_run(resumed.run_id)
    resumed_artifact_dir = Path(resumed_loaded.artifact_dir)
    resumed_jobs = summarize_jobs(resumed_loaded, resumed_artifact_dir)
    resumed_types = event_types(store, resumed.run_id)

    source_ok = (
        source_loaded.status == "failed"
        and source_error is not None
        and "GA_P8_FORCED_SOURCE_FAILURE" in source_error
        and source_runner.started_job_ids == ["agent_1"]
        and len(source_loaded.jobs) == 1
        and source_jobs[0]["status"] == "succeeded"
        and source_jobs[0]["resultExists"]
        and source_jobs[0]["transcriptExists"]
        and "workflow_failed" in source_types
    )
    resume_ok = (
        resumed_loaded.status == "succeeded"
        and resume_runner.started_job_ids == ["agent_2"]
        and len(resumed_loaded.jobs) == 2
        and resumed_jobs[0]["status"] == "cached"
        and resumed_jobs[0]["cachedFromRunId"] == source_loaded.run_id
        and resumed_jobs[0]["cachedFromJobId"] == "agent_1"
        and resumed_jobs[0]["resultExists"]
        and resumed_jobs[0]["transcriptExists"]
        and resumed_jobs[1]["status"] == "succeeded"
        and resumed_jobs[1]["resultExists"]
        and resumed_jobs[1]["transcriptExists"]
        and resumed_types.count("agent_cached") == 1
        and resumed_types.count("agent_started") == 1
        and resumed_types.count("agent_completed") == 1
        and (resume_outcome.result or {}).get("marker") == "GA_P8_FAILED_RESUME_DONE"
    )
    return {
        "passed": source_ok and resume_ok,
        "elapsedSeconds": round(elapsed, 2),
        "sourceRunId": source_loaded.run_id,
        "sourceStatus": source_loaded.status,
        "sourceError": sanitize(source_error),
        "sourceStartedJobIds": source_runner.started_job_ids,
        "sourceEventCounts": {etype: source_types.count(etype) for etype in sorted(set(source_types))},
        "sourceJobs": source_jobs,
        "resumedRunId": resumed_loaded.run_id,
        "resumedStatus": resumed_loaded.status,
        "resumedStartedJobIds": resume_runner.started_job_ids,
        "resumedEventCounts": {etype: resumed_types.count(etype) for etype in sorted(set(resumed_types))},
        "resumedJobs": resumed_jobs,
        "resumeResultShape": sanitize(resume_outcome.result),
        "artifactDir": str(source_artifact_dir),
        "resumedArtifactDir": str(resumed_artifact_dir),
    }



def run_killed_source_resume_real_api_case(root: Path) -> dict:
    store = WorkflowStore(root / "killed_resume")
    source = store.create_run(WorkflowRun(run_id="wf_p8_killed_source", session_id="p8_real_api_killed_resume", script=KILLED_SOURCE_SCRIPT, status="running"))
    source_runner = GateOnSecondStartRunner(config_name=CONFIG_NAME, max_tokens=96)
    source_runtime = WorkflowRuntime(
        store=store,
        runner=source_runner,
        scheduler_config=SchedulerConfig(max_concurrent=1, max_total=4),
        timeout_seconds=360.0,
    )
    errors: list[str] = []
    start = time.time()

    def run_source():
        try:
            source_runtime.run(source, args={"suite": "p8-real-api", "case": "killed-resume"})
        except Exception as exc:
            errors.append(f"{type(exc).__name__}: {exc}")

    thread = threading.Thread(target=run_source, daemon=True)
    thread.start()
    second_seen = False
    second_deadline = time.time() + 240.0
    while time.time() < second_deadline:
        try:
            current = store.load_run(source.run_id)
            source_events = store.replay_events(source.run_id)
        except Exception:
            time.sleep(0.05)
            continue
        second_seen = any(event.event_type == "agent_started" and event.job_id == "agent_2" for event in source_events) and any(job.job_id == "agent_2" and job.status == "running" for job in current.jobs)
        if second_seen:
            break
        time.sleep(0.05)
    if second_seen:
        killed = store.load_run(source.run_id)
        killed.status = "killed"
        killed.error = "GA_P8_FORCED_SOURCE_KILL"
        store.save_run(killed)
        deadline = time.time() + 30.0
        while time.time() < deadline:
            current = store.load_run(source.run_id)
            if current.status == "killed" and any(job.status == "cancelled" for job in current.jobs):
                break
            time.sleep(0.05)
    elapsed = time.time() - start
    source_loaded = store.load_run(source.run_id)
    source_artifact_dir = Path(source_loaded.artifact_dir)
    source_jobs = summarize_jobs(source_loaded, source_artifact_dir)
    source_types = event_types(store, source.run_id)

    resumed = store.create_run(WorkflowRun(run_id="wf_p8_killed_resumed", session_id="p8_real_api_killed_resume", script=RESUME_AFTER_KILLED_SCRIPT, status="running"))
    resume_runner = CountingNativeRunner(config_name=CONFIG_NAME, max_tokens=96)
    resume_runtime = WorkflowRuntime(
        store=store,
        runner=resume_runner,
        scheduler_config=SchedulerConfig(max_concurrent=1, max_total=4),
        timeout_seconds=240.0,
    )
    resume_outcome = resume_runtime.run(resumed, args={"suite": "p8-real-api", "case": "killed-resume"}, resume_from_run_id=source_loaded.run_id)
    resumed_loaded = store.load_run(resumed.run_id)
    resumed_artifact_dir = Path(resumed_loaded.artifact_dir)
    resumed_jobs = summarize_jobs(resumed_loaded, resumed_artifact_dir)
    resumed_types = event_types(store, resumed.run_id)

    source_ok = (
        second_seen
        and not thread.is_alive()
        and source_loaded.status == "killed"
        and bool(errors)
        and "GA_P8_FORCED_SOURCE_KILL" in errors[0]
        and source_runner.started_job_ids == ["agent_1", "agent_2"]
        and len(source_loaded.jobs) == 2
        and source_jobs[0]["status"] == "succeeded"
        and source_jobs[0]["resultExists"]
        and source_jobs[0]["transcriptExists"]
        and source_jobs[1]["status"] == "cancelled"
        and "workflow_killed" in source_types
    )
    resume_ok = (
        resumed_loaded.status == "succeeded"
        and resume_runner.started_job_ids == ["agent_2"]
        and len(resumed_loaded.jobs) == 2
        and resumed_jobs[0]["status"] == "cached"
        and resumed_jobs[0]["cachedFromRunId"] == source_loaded.run_id
        and resumed_jobs[0]["cachedFromJobId"] == "agent_1"
        and resumed_jobs[0]["resultExists"]
        and resumed_jobs[0]["transcriptExists"]
        and resumed_jobs[1]["status"] == "succeeded"
        and resumed_jobs[1]["resultExists"]
        and resumed_jobs[1]["transcriptExists"]
        and resumed_types.count("agent_cached") == 1
        and resumed_types.count("agent_started") == 1
        and resumed_types.count("agent_completed") == 1
        and (resume_outcome.result or {}).get("marker") == "GA_P8_KILLED_RESUME_DONE"
    )
    return {
        "passed": source_ok and resume_ok,
        "elapsedSeconds": round(elapsed, 2),
        "sourceRunId": source_loaded.run_id,
        "sourceStatus": source_loaded.status,
        "sourceErrors": sanitize(errors),
        "sourceSecondStarted": second_seen,
        "sourceThreadAlive": thread.is_alive(),
        "sourceStartedJobIds": source_runner.started_job_ids,
        "sourceCancelledJobIds": source_runner.cancelled_job_ids,
        "sourceEventCounts": {etype: source_types.count(etype) for etype in sorted(set(source_types))},
        "sourceJobs": source_jobs,
        "resumedRunId": resumed_loaded.run_id,
        "resumedStatus": resumed_loaded.status,
        "resumedStartedJobIds": resume_runner.started_job_ids,
        "resumedEventCounts": {etype: resumed_types.count(etype) for etype in sorted(set(resumed_types))},
        "resumedJobs": resumed_jobs,
        "resumeResultShape": sanitize(resume_outcome.result),
        "artifactDir": str(source_artifact_dir),
        "resumedArtifactDir": str(resumed_artifact_dir),
    }



def run_bridge_real_api_case(root: Path) -> dict:
    events: list[dict] = []
    runtime_started_jobs: list[str] = []

    def runtime_factory(*, store, timeout_seconds=10.0):
        runner = CountingNativeRunner(config_name=CONFIG_NAME, max_tokens=64)
        runtime_started_jobs_ref = runner.started_job_ids
        runtime = WorkflowRuntime(
            store=store,
            runner=runner,
            scheduler_config=SchedulerConfig(max_concurrent=1, max_total=4),
            timeout_seconds=180.0,
        )
        original_run = runtime.run

        def run_and_record(*args, **kwargs):
            result = original_run(*args, **kwargs)
            runtime_started_jobs.extend(runtime_started_jobs_ref)
            return result

        runtime.run = run_and_record
        return runtime

    bridge = GenericAgentBridge(
        agent_factory=MinimalAgent,
        emit=events.append,
        workflow_root=root / "bridge",
        workflow_runtime_factory=runtime_factory,
    )
    source_run_id = bridge.workflow_draft(BRIDGE_SCRIPT)
    approved = bridge.workflow_approve(source_run_id, args={"suite": "p8-real-api", "case": "bridge"}, timeout_seconds=180.0)
    bridge.wait_for_workflow_idle(source_run_id, timeout=220.0)
    final_events = [event for event in events if event.get("type") == "workflow_final"]
    workflow_events = [event.get("event", {}).get("type") for event in events if event.get("type") == "workflow_event"]
    run_events = [event.get("run", {}) for event in events if event.get("type") in {"workflow_draft", "workflow_run"}]
    final_result = final_events[-1].get("result") if final_events else {}
    passed = (
        bool(source_run_id)
        and approved
        and bool(final_events)
        and final_result.get("status") == "succeeded"
        and runtime_started_jobs == ["agent_1"]
        and {"workflow_approval_requested", "workflow_started", "workflow_phase", "workflow_log", "agent_registered", "agent_started", "agent_completed"}.issubset(set(workflow_events))
    )
    try:
        bridge.stop()
    except Exception:
        pass
    return {
        "passed": passed,
        "runId": source_run_id,
        "approved": approved,
        "startedJobIds": runtime_started_jobs,
        "eventTypes": [event.get("type") for event in events],
        "workflowEventTypes": workflow_events,
        "runStatuses": [run.get("status") for run in run_events],
        "finalStatus": final_result.get("status"),
        "finalResultShape": sanitize(final_result.get("result")),
    }


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="ga_p8_real_api_e2e_"))
    summary: dict[str, Any] = {
        "passed": False,
        "skipped": False,
        "configName": CONFIG_NAME,
        "expectedProfileName": EXPECTED_PROFILE_NAME,
        "expectedModel": EXPECTED_MODEL,
        "root": str(root),
        "cases": {},
        "secretScan": [],
        "error": None,
    }
    if not OPT_IN:
        summary.update({"skipped": True, "reason": "set GA_RUN_REAL_API_E2E=1 to run real API workflow E2E"})
        print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
        return 0
    try:
        if not check_profile(summary):
            summary["error"] = "profile mismatch; expected gpt-native / gpt-5.5"
            return 2
        captured = io.StringIO()
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            runtime_case = run_runtime_real_api_case(root)
            failed_resume_case = run_failed_source_resume_real_api_case(root)
            killed_resume_case = run_killed_source_resume_real_api_case(root)
            bridge_case = run_bridge_real_api_case(root)
        summary["capturedLogChars"] = len(captured.getvalue())
        summary["cases"]["runtimeAgentParallelPipelineResume"] = runtime_case
        summary["cases"]["failedSourceResumeLongestPrefix"] = failed_resume_case
        summary["cases"]["killedSourceResumeLongestPrefix"] = killed_resume_case
        summary["cases"]["bridgeDraftApproveFinal"] = bridge_case
        summary["secretScan"] = scan_for_secret_material(root)
        summary["passed"] = runtime_case.get("passed") and failed_resume_case.get("passed") and killed_resume_case.get("passed") and bridge_case.get("passed") and not summary["secretScan"]
        return 0 if summary["passed"] else 2
    except Exception as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
        return 1
    finally:
        print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
