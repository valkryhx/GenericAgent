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
from unittest import mock

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
REAL_MCP_OPT_IN = os.environ.get("GA_RUN_REAL_MCP_E2E") == "1"

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

INHERIT_PERMISSION_SMOKE_SCRIPT = r'''
phase('P8 Real API Permission Inheritance Smoke');
log('start p8 real api permission inheritance smoke');
const result = await agent('Return one short safe sentence. Do not call tools, MCP, or skills. Avoid secrets. Include token GA_P8_INHERIT_PERMISSION_SMOKE if natural.', {label:'inherit-permission-smoke'});
return {
  marker: 'GA_P8_INHERIT_PERMISSION_SMOKE_DONE',
  summaryLength: String(result.summary || '').length
};
'''

TOOL_INHERITANCE_SCRIPT = r'''
phase('P8 Real API Native Tool Inheritance File Skill MCP');
log('start p8 real api native child file skill mcp inheritance e2e');
const result = await agent(`You must prove NativeGPTChildAgentRunner tool calling by using tools before the final answer.
Use exactly these three tools in this order:
1. file_read with path '${args.markerPath}' and show_linenos=false.
2. load_skill with skill 'p8-real-tool-skill' and search_roots ['${args.skillRoot}'].
3. mcp__p8_stub__read_marker with marker 'GA_P8_REAL_TOOL_MCP_INPUT'.
After all three tool calls, return one short sentence containing GA_P8_REAL_TOOL_INHERITANCE_DONE. Do not write files, execute code, access network, read mykey.py, read mykey.json, read mcp.json, or call any other tool.`, {
  label:'real-tool-inheritance-file-skill-mcp'
});
return {
  marker: 'GA_P8_REAL_TOOL_INHERITANCE_DONE',
  summaryLength: String(result.summary || '').length
};
'''

REAL_MCP_DIAGNOSTIC_SCRIPT = r'''
phase('P8 Real MCP Diagnostic');
log('start p8 real non-mock mcp diagnostic');
const result = await agent(`Use the real MCP tool ${args.toolName} exactly once before final answer.
Call it with exactly this JSON argument object: ${args.toolArgsJson}.
After the tool call, return one short sentence containing token GA_P8_REAL_MCP_DIAGNOSTIC_DONE.
Do not call file tools, do not call skills, do not read mykey.py, mykey.json, or mcp.json, and do not call any other tool.`, {
  label:'real-mcp-diagnostic'
});
return {
  marker: 'GA_P8_REAL_MCP_DIAGNOSTIC_DONE',
  summaryLength: String(result.summary || '').length
};
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


def _make_tool_inheritance_fixture(root: Path) -> tuple[Path, Path]:
    fixture = root / "tool_inheritance_fixture"
    fixture.mkdir(parents=True, exist_ok=True)
    marker_path = fixture / "marker.txt"
    marker_path.write_text("GA_P8_REAL_TOOL_FILE_READ_MARKER\n", encoding="utf-8")
    skill_root = fixture / "skills"
    skill_dir = skill_root / "p8-real-tool-skill"
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(
        "---\nname: p8-real-tool-skill\ndescription: P8 real API tool inheritance smoke skill\nallowed-tools: [file_read, mcp__p8_stub__read_marker]\n---\n# P8 Real Tool Skill\nReturn the skill marker GA_P8_REAL_TOOL_SKILL_MARKER when summarizing this skill.\n",
        encoding="utf-8",
    )
    return marker_path, skill_root


def run_inherit_permission_smoke_case(root: Path) -> dict:
    store = WorkflowStore(root / "inherit_permission_smoke")
    run = store.create_run(WorkflowRun(run_id="wf_p8_inherit_permission_smoke", session_id="p8_real_api_inherit_permission", script=INHERIT_PERMISSION_SMOKE_SCRIPT, status="running"))
    runner = CountingNativeRunner(config_name=CONFIG_NAME, max_tokens=64)
    runtime = WorkflowRuntime(
        store=store,
        runner=runner,
        scheduler_config=SchedulerConfig(max_concurrent=1, max_total=2),
        timeout_seconds=180.0,
    )
    start = time.time()
    outcome = runtime.run(run, args={"suite": "p8-real-api", "case": "inherit-permission-smoke"})
    elapsed = time.time() - start
    loaded = store.load_run(run.run_id)
    artifact_dir = Path(loaded.artifact_dir)
    jobs = summarize_jobs(loaded, artifact_dir)
    job = loaded.jobs[0] if loaded.jobs else None
    result_path = artifact_dir / (job.result_ref or f"agents/{job.job_id}/result.json") if job else artifact_dir / "missing.json"
    result_data = load_json(result_path) if result_path.exists() else {}
    transcript_ref = (job.metadata.get("transcriptRef") if job else None) or result_data.get("transcriptRef")
    transcript_path = artifact_dir / transcript_ref if transcript_ref else None
    transcript_events = read_jsonl(transcript_path) if transcript_path and transcript_path.exists() else []
    metadata_events = [event for event in transcript_events if event.get("type") == "metadata"]
    metadata = metadata_events[0] if metadata_events else {}
    event_types_for_run = event_types(store, loaded.run_id)
    cache_key = (job.metadata.get("cacheKey") if job else {}) or {}
    payload = result_data.get("payload") or {}
    passed = (
        outcome.run.status == "succeeded"
        and loaded.status == "succeeded"
        and runner.started_job_ids == ["agent_1"]
        and len(loaded.jobs) == 1
        and bool(job and job.status == "succeeded")
        and cache_key.get("permissionProfile") == "inherit-current-permissions"
        and cache_key.get("permissionPolicyVersion") == "inherit-current-v1"
        and metadata.get("permissionProfile") == "inherit-current-permissions"
        and metadata.get("permissionPolicyVersion") == "inherit-current-v1"
        and metadata.get("configName") == CONFIG_NAME
        and "transcriptEvents" not in result_data
        and set((result_data.get("toolSummary") or {}).get("allowedTools") or []) <= {"no_tool"}
        and (result_data.get("toolSummary") or {}).get("denied", 0) == 0
        and len(str(payload.get("summary") or "")) > 0
        and jobs
        and jobs[0].get("resultExists")
        and jobs[0].get("transcriptExists")
    )
    return {
        "passed": passed,
        "elapsedSeconds": round(elapsed, 2),
        "runId": loaded.run_id,
        "status": loaded.status,
        "startedJobIds": runner.started_job_ids,
        "eventCounts": {etype: event_types_for_run.count(etype) for etype in sorted(set(event_types_for_run))},
        "jobs": jobs,
        "permissionProfile": cache_key.get("permissionProfile"),
        "permissionPolicyVersion": cache_key.get("permissionPolicyVersion"),
        "metadataPermissionProfile": metadata.get("permissionProfile"),
        "metadataPermissionPolicyVersion": metadata.get("permissionPolicyVersion"),
        "metadataConfigName": metadata.get("configName"),
        "resultJsonOmitsTranscriptEvents": "transcriptEvents" not in result_data,
        "toolSummary": result_data.get("toolSummary"),
        "summaryLength": len(str(payload.get("summary") or "")),
        "artifactDir": str(artifact_dir),
    }



def _minimal_tool_inheritance_schema(mcp_schema: dict) -> list[dict]:
    tools = json.loads((REPO / "assets" / "tools_schema.json").read_text(encoding="utf-8"))
    selected = [tool for tool in tools if (tool.get("function") or {}).get("name") in {"file_read", "load_skill"}]
    selected.append(mcp_schema)
    return selected


def run_tool_inheritance_real_api_case(root: Path) -> dict:
    marker_path, skill_root = _make_tool_inheritance_fixture(root)
    store = WorkflowStore(root / "tool_inheritance_smoke")
    run = store.create_run(WorkflowRun(run_id="wf_p8_tool_inheritance_smoke", session_id="p8_real_api_tool_inheritance", script=TOOL_INHERITANCE_SCRIPT, status="running", permission_profile="read_only", permission_policy_version="read-only-v1"))
    mcp_schema = {
        "type": "function",
        "function": {
            "name": "mcp__p8_stub__read_marker",
            "description": "Read a deterministic marker from the P8 in-process MCP stub.",
            "parameters": {
                "type": "object",
                "properties": {"marker": {"type": "string"}},
                "required": ["marker"],
            },
        },
    }
    runner = CountingNativeRunner(config_name=CONFIG_NAME, max_tokens=512, max_turns=12, tools_schema_factory=lambda: _minimal_tool_inheritance_schema(mcp_schema))
    runtime = WorkflowRuntime(
        store=store,
        runner=runner,
        scheduler_config=SchedulerConfig(max_concurrent=1, max_total=2),
        timeout_seconds=360.0,
    )
    start = time.time()
    with mock.patch("mcp_runtime.discover_mcp_tools_cached", return_value=[mcp_schema]), mock.patch("mcp_runtime.call_mcp_tool", return_value={"status": "success", "marker": "GA_P8_REAL_TOOL_MCP_MARKER", "input": "GA_P8_REAL_TOOL_MCP_INPUT"}) as call_mcp:
        outcome = runtime.run(run, args={"suite": "p8-real-api", "case": "native-tool-calling-file-skill-mcp", "markerPath": str(marker_path), "skillRoot": str(skill_root)})
    elapsed = time.time() - start
    loaded = store.load_run(run.run_id)
    artifact_dir = Path(loaded.artifact_dir)
    job = loaded.jobs[0] if loaded.jobs else None
    result_path = artifact_dir / (job.result_ref or f"agents/{job.job_id}/result.json") if job else artifact_dir / "missing.json"
    result_data = load_json(result_path) if result_path.exists() else {}
    transcript_ref = (job.metadata.get("transcriptRef") if job else None) or result_data.get("transcriptRef")
    transcript_path = artifact_dir / transcript_ref if transcript_ref else None
    transcript_events = read_jsonl(transcript_path) if transcript_path and transcript_path.exists() else []
    event_types_for_run = event_types(store, loaded.run_id)
    tool_calls = [event.get("toolName") for event in transcript_events if event.get("type") == "tool_call"]
    tool_results = [event for event in transcript_events if event.get("type") == "tool_result"]
    allowed_tools = [event.get("toolName") for event in transcript_events if event.get("type") == "tool_allowed"]
    tool_summary = result_data.get("toolSummary") or {}
    payload = result_data.get("payload") or {}
    denied_tools = [event.get("toolName") for event in transcript_events if event.get("type") == "tool_denied"]
    expected_tools = {"file_read", "load_skill", "mcp__p8_stub__read_marker"}
    skill_ok = any(event.get("toolName") == "load_skill" and (event.get("data") or {}).get("status") == "success" and (event.get("data") or {}).get("name") == "p8-real-tool-skill" and "GA_P8_REAL_TOOL_SKILL_MARKER" in str(event.get("data")) for event in tool_results)
    file_ok = any(event.get("toolName") == "file_read" and "GA_P8_REAL_TOOL_FILE_READ_MARKER" in str(event.get("data")) for event in tool_results)
    mcp_ok = any(event.get("toolName") == "mcp__p8_stub__read_marker" and "GA_P8_REAL_TOOL_MCP_MARKER" in str(event.get("data")) for event in tool_results)
    passed = (
        outcome.run.status == "succeeded"
        and loaded.status == "succeeded"
        and runner.started_job_ids == ["agent_1"]
        and bool(job and job.status == "succeeded")
        and expected_tools.issubset(set(tool_calls))
        and expected_tools.issubset(set(allowed_tools))
        and expected_tools.issubset(set(tool_summary.get("allowedTools") or []))
        and tool_summary.get("denied") == 0
        and not denied_tools
        and file_ok
        and skill_ok
        and mcp_ok
        and call_mcp.call_count == 1
        and "transcriptEvents" not in result_data
        and len(str(payload.get("summary") or "")) > 0
    )
    return {
        "passed": passed,
        "elapsedSeconds": round(elapsed, 2),
        "runId": loaded.run_id,
        "status": loaded.status,
        "startedJobIds": runner.started_job_ids,
        "eventCounts": {etype: event_types_for_run.count(etype) for etype in sorted(set(event_types_for_run))},
        "toolCalls": tool_calls,
        "allowedTools": allowed_tools,
        "deniedTools": denied_tools,
        "toolSummary": tool_summary,
        "fileReadOk": file_ok,
        "skillLoadOk": skill_ok,
        "mcpReadOk": mcp_ok,
        "mcpCallCount": call_mcp.call_count,
        "resultJsonOmitsTranscriptEvents": "transcriptEvents" not in result_data,
        "summaryLength": len(str(payload.get("summary") or "")),
        "artifactDir": str(artifact_dir),
    }


def _pick_real_mcp_diagnostic_tool() -> tuple[str | None, dict, dict]:
    try:
        import mcp_runtime
        mcp_runtime.clear_mcp_cache()
        mcp_runtime.reset_mcp_manager()
        tools = mcp_runtime.discover_mcp_tools_cached(timeout=20)
    except Exception as exc:
        return None, {}, {"error": f"{type(exc).__name__}: {exc}"}
    schemas_by_name = {(tool.get("function") or {}).get("name") or "": tool for tool in tools}
    names = sorted(name for name in schemas_by_name if name)
    preferred = [
        ("mcp__fetch__fetch", {"url": "https://example.com", "max_length": 200}),
        ("mcp__context7__resolve-library-id", {"libraryName": "React", "query": "Resolve React docs for P8 real MCP diagnostic."}),
        ("mcp__tavily__tavily_search", {"query": "React official documentation", "max_results": 1, "search_depth": "basic", "topic": "general"}),
        ("mcp__exa__web_search_exa", {"query": "React official documentation", "numResults": 1}),
        ("mcp__memory__search_nodes", {"query": "P8 real MCP diagnostic"}),
    ]
    for name, args in preferred:
        if name in names:
            return name, args, {"availableToolCount": len(names), "selectedFrom": "preferred", "schema": schemas_by_name[name]}
    for name in names:
        if "search" in name or "query" in name or "resolve" in name:
            return name, {"query": "P8 real MCP diagnostic"}, {"availableToolCount": len(names), "selectedFrom": "fallback", "schema": schemas_by_name[name]}
    return None, {}, {"availableToolCount": len(names), "availableToolsSample": names[:20], "error": "no suitable real MCP diagnostic tool discovered"}


def run_real_mcp_diagnostic_case(root: Path) -> dict:
    if not REAL_MCP_OPT_IN:
        return {"passed": False, "skipped": True, "reason": "set GA_RUN_REAL_MCP_E2E=1 to run non-mock real MCP diagnostic"}
    tool_name, tool_args, discovery = _pick_real_mcp_diagnostic_tool()
    if not tool_name:
        return {"passed": False, "skipped": True, "reason": discovery.get("error") or "no real MCP tool discovered", "discovery": sanitize(discovery)}
    selected_schema = discovery.pop("schema", None)
    if not selected_schema:
        return {"passed": False, "skipped": True, "reason": "selected real MCP schema is missing", "discovery": sanitize(discovery)}
    store = WorkflowStore(root / "real_mcp_diagnostic")
    run = store.create_run(WorkflowRun(run_id="wf_p8_real_mcp_diagnostic", session_id="p8_real_api_real_mcp", script=REAL_MCP_DIAGNOSTIC_SCRIPT, status="running", permission_profile="read_only", permission_policy_version="read-only-v1"))
    runner = CountingNativeRunner(config_name=CONFIG_NAME, max_tokens=384, max_turns=8, tools_schema_factory=lambda: [selected_schema])
    runtime = WorkflowRuntime(store=store, runner=runner, scheduler_config=SchedulerConfig(max_concurrent=1, max_total=2), timeout_seconds=360.0)
    start = time.time()
    error = None
    try:
        outcome = runtime.run(run, args={"suite": "p8-real-api", "case": "real-mcp-diagnostic", "toolName": tool_name, "toolArgsJson": json.dumps(tool_args, ensure_ascii=False)})
    except Exception as exc:
        outcome = None
        error = f"{type(exc).__name__}: {exc}"
    elapsed = time.time() - start
    loaded = store.load_run(run.run_id)
    artifact_dir = Path(loaded.artifact_dir)
    job = loaded.jobs[0] if loaded.jobs else None
    result_path = artifact_dir / (job.result_ref or f"agents/{job.job_id}/result.json") if job else artifact_dir / "missing.json"
    result_data = load_json(result_path) if result_path.exists() else {}
    transcript_ref = (job.metadata.get("transcriptRef") if job else None) or result_data.get("transcriptRef")
    transcript_path = artifact_dir / transcript_ref if transcript_ref else None
    transcript_events = read_jsonl(transcript_path) if transcript_path and transcript_path.exists() else []
    tool_calls = [event.get("toolName") for event in transcript_events if event.get("type") == "tool_call"]
    tool_results = [event for event in transcript_events if event.get("type") == "tool_result"]
    allowed_tools = [event.get("toolName") for event in transcript_events if event.get("type") == "tool_allowed"]
    denied_tools = [event.get("toolName") for event in transcript_events if event.get("type") == "tool_denied"]
    tool_result_text = "\n".join(str(event.get("data"))[:1000] for event in tool_results if event.get("toolName") == tool_name)
    mcp_called = tool_name in tool_calls
    mcp_returned = any(event.get("toolName") == tool_name for event in tool_results)
    return {
        "passed": bool((outcome is not None and outcome.run.status == "succeeded") and loaded.status == "succeeded" and mcp_called and mcp_returned and not denied_tools),
        "diagnosticOnly": True,
        "skipped": False,
        "elapsedSeconds": round(elapsed, 2),
        "runId": loaded.run_id,
        "status": loaded.status,
        "error": sanitize(error),
        "selectedTool": tool_name,
        "selectedArgs": sanitize(tool_args),
        "discovery": sanitize(discovery),
        "startedJobIds": runner.started_job_ids,
        "toolCalls": tool_calls,
        "allowedTools": allowed_tools,
        "deniedTools": denied_tools,
        "toolSummary": result_data.get("toolSummary"),
        "mcpCalled": mcp_called,
        "mcpReturned": mcp_returned,
        "toolResultPreview": sanitize(tool_result_text[:1000]),
        "artifactDir": str(artifact_dir),
    }


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


def run_case_safely(case_name: str, fn, *args, diagnostic_only: bool = False, **kwargs) -> dict:
    start = time.time()
    try:
        result = fn(*args, **kwargs)
        if not isinstance(result, dict):
            return {
                "passed": False,
                "skipped": False,
                "diagnosticOnly": diagnostic_only,
                "error": f"case returned non-dict: {type(result).__name__}",
                "exceptionType": "InvalidCaseResult",
                "elapsedSeconds": round(time.time() - start, 2),
            }
        result.setdefault("passed", False)
        result.setdefault("skipped", False)
        if diagnostic_only:
            result.setdefault("diagnosticOnly", True)
        return sanitize(result)
    except Exception as exc:
        return {
            "passed": False,
            "skipped": False,
            "diagnosticOnly": diagnostic_only,
            "error": sanitize(str(exc)),
            "exceptionType": type(exc).__name__,
            "elapsedSeconds": round(time.time() - start, 2),
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
        "diagnostics": {},
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
            summary["cases"]["inheritPermissionSmoke"] = run_case_safely("inheritPermissionSmoke", run_inherit_permission_smoke_case, root)
            summary["cases"]["nativeToolCallingFileSkillMcp"] = run_case_safely("nativeToolCallingFileSkillMcp", run_tool_inheritance_real_api_case, root)
            summary["cases"]["runtimeAgentParallelPipelineResume"] = run_case_safely("runtimeAgentParallelPipelineResume", run_runtime_real_api_case, root)
            summary["cases"]["failedSourceResumeLongestPrefix"] = run_case_safely("failedSourceResumeLongestPrefix", run_failed_source_resume_real_api_case, root)
            summary["cases"]["killedSourceResumeLongestPrefix"] = run_case_safely("killedSourceResumeLongestPrefix", run_killed_source_resume_real_api_case, root)
            summary["cases"]["bridgeDraftApproveFinal"] = run_case_safely("bridgeDraftApproveFinal", run_bridge_real_api_case, root)
            if REAL_MCP_OPT_IN:
                summary["diagnostics"]["realMcpDiagnostic"] = run_case_safely("realMcpDiagnostic", run_real_mcp_diagnostic_case, root, diagnostic_only=True)
        summary["capturedLogChars"] = len(captured.getvalue())
        summary["secretScan"] = scan_for_secret_material(root)
        required_cases_passed = all(case.get("passed") for case in summary["cases"].values())
        summary["passed"] = required_cases_passed and not summary["secretScan"]
        return 0 if summary["passed"] else 2
    except Exception as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
        return 1
    finally:
        print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    raise SystemExit(main())
