from __future__ import annotations

import datetime as dt
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
OPT_IN = os.environ.get('GA_RUN_REAL_E2E') == '1'
MCP_OPT_IN = os.environ.get('GA_RUN_REAL_MCP_E2E') == '1'
PROFILE = os.environ.get('GA_WORKFLOW_LLM_PROFILE', 'deepseek-v4.1-flash')
EXPECTED_MODEL = os.environ.get('GA_REAL_API_EXPECTED_MODEL', 'deepseek-v4.1-flash')
EXPECTED_NAME = os.environ.get('GA_REAL_API_EXPECTED_NAME', 'deepseek-v4.1-flash')

WORKFLOW_SCRIPT = r'''
phase('Module analysis');
const analysis = await agent(`Read ${args.sourcePath} with file_read. ${args.mcpInstruction}Write ${args.workspacePath}/analysis.md with two concrete boundary conditions using file_write. Do not modify the repository. Final answer must contain GA_BARRIER_ANALYSIS_DONE.`, {label:'module-analysis'});
phase('Test suggestions');
const suggestions = await agent(`Read ${args.workspacePath}/analysis.md with file_read, then use prior result ${JSON.stringify(analysis)} to write ${args.workspacePath}/test_suggestions.md with focused test ideas. Do not modify the repository. Final answer must contain GA_BARRIER_SUGGESTIONS_DONE.`, {label:'test-suggestions'});
return {marker:'GA_WAIT_BARRIER_WORKFLOW_DONE', analysisLength:String(analysis.summary || '').length, suggestionsLength:String(suggestions.summary || '').length};
'''


def _profile():
    from llm_client import load_clients_from_yaml
    clients, *_ = load_clients_from_yaml(start_dir=REPO)
    for index, client in enumerate(clients):
        backend = getattr(client, 'backend', None)
        name, model = getattr(backend, 'name', ''), getattr(backend, 'model', '')
        if model == EXPECTED_MODEL and (not EXPECTED_NAME or EXPECTED_NAME in {'*', name} or EXPECTED_NAME in name):
            return index, {'name': name, 'model': model}
    raise RuntimeError(f'profile not found: {EXPECTED_NAME}/{EXPECTED_MODEL}')


def _load(path: Path):
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError):
        return {}


def _load_line(line):
    try:
        return json.loads(line)
    except json.JSONDecodeError:
        return {}


def _timestamp(value):
    if not value:
        return None
    try:
        return dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _mcp_duration_ms(events):
    calls = [_timestamp(event.get("ts")) for event in events if event.get("type") == "tool_call" and event.get("toolName") == "mcp__tavily__tavily_search"]
    results = [_timestamp(event.get("ts")) for event in events if event.get("type") == "tool_result" and event.get("toolName") == "mcp__tavily__tavily_search"]
    if calls and results and calls[0] is not None and results[0] is not None:
        return round((results[0] - calls[0]) * 1000, 2)
    return None


@unittest.skipUnless(OPT_IN, 'set GA_RUN_REAL_E2E=1 for live DeepSeek workflow E2E')
class RealWorkflowWaitBarrierE2E(unittest.TestCase):
    def test_real_workflow_enforces_dependency_barrier_and_host_evidence(self):
        from workflow_check_adapters import run_check
        from workflow_child_agent import NativeGPTChildAgentRunner
        from workflow_models import WorkflowRun
        from workflow_runtime import WorkflowRuntime
        from workflow_scheduler import SchedulerConfig
        from workflow_store import WorkflowStore

        _llm_no, profile = _profile()
        if MCP_OPT_IN:
            import mcp_runtime
            mcp_runtime.clear_mcp_cache()
            mcp_runtime.reset_mcp_manager()
            tools = mcp_runtime.discover_mcp_tools_cached(timeout=30)
            names = {(tool.get('function') or {}).get('name') for tool in tools}
            self.assertIn('mcp__tavily__tavily_search', names)
        root = Path(tempfile.mkdtemp(prefix='ga_real_wait_barrier_'))
        workspace = root / 'workspace'
        workspace.mkdir(parents=True)
        store = WorkflowStore(root / 'runtime')
        run = store.create_run(WorkflowRun(
            run_id='wf_real_wait_barrier', session_id='real_wait_barrier',
            script=WORKFLOW_SCRIPT, status='running',
            metadata={'workflowName': 'real-wait-barrier-e2e', 'profile': profile, 'mcpRequired': MCP_OPT_IN},
        ))
        runner = NativeGPTChildAgentRunner(profile_name=PROFILE, max_tokens=1024, max_turns=12)
        started = time.monotonic()
        metrics = {
            'profile': profile, 'mcpRequired': MCP_OPT_IN,
            'waitPredicates': ['workflow_terminal'], 'duplicateSpawnCount': 0,
            'failureCategories': [], 'waitReturnCount': 1,
            'spawnToProcessEntryMs': None, 'processEntryToTurnStartedMs': None,
            'turnDurationMs': None, 'mcpDurationMs': None, 'resultPersistenceLatencyMs': None,
        }
        try:
            instruction = (
                'You MUST call mcp__tavily__tavily_search exactly once with query "Python workflow scheduler barrier testing" before final answer. '
                if MCP_OPT_IN else 'Do not call MCP. '
            )
            outcome = WorkflowRuntime(
                store=store, runner=runner,
                scheduler_config=SchedulerConfig(max_concurrent=1, max_total=4),
                timeout_seconds=900,
            ).run(run, args={
                'workspacePath': str(workspace),
                'sourcePath': str(REPO / 'workflow_planner.py'),
                'mcpInstruction': instruction,
            })
            loaded = store.load_run(run.run_id)
            journal = store.replay_events(run.run_id)
            metrics['workflowTotalLatencyMs'] = round((time.monotonic() - started) * 1000, 2)
            metrics['jobStatuses'] = [job.status for job in loaded.jobs]
            metrics['journalEvents'] = [event.event_type for event in journal]
            labels = {str(job.metadata.get('label')): job.job_id for job in loaded.jobs}
            starts = {event.job_id: event.sequence for event in journal if event.event_type == 'agent_started'}
            completes = {event.job_id: event.sequence for event in journal if event.event_type == 'agent_completed'}
            self.assertEqual('succeeded', loaded.status)
            self.assertEqual('succeeded', outcome.run.status)
            self.assertEqual(2, len(loaded.jobs))
            analysis_id, suggestion_id = labels.get('module-analysis'), labels.get('test-suggestions')
            self.assertIsNotNone(analysis_id)
            self.assertIsNotNone(suggestion_id)
            self.assertLess(completes[analysis_id], starts[suggestion_id], 'downstream started before upstream terminal')
            metrics['barrier'] = {'upstreamCompletedSequence': completes[analysis_id], 'downstreamStartedSequence': starts[suggestion_id], 'satisfied': True}
            checks = [
                run_check({'id': 'analysis-artifact', 'kind': 'artifact', 'required': True, 'path': 'analysis.md'}, workspace=workspace),
                run_check({'id': 'suggestions-artifact', 'kind': 'artifact', 'required': True, 'path': 'test_suggestions.md'}, workspace=workspace),
                run_check({'id': 'workspace-compile', 'kind': 'command', 'required': True, 'command': ['python', '-m', 'compileall', '-q', str(workspace)]}, workspace=workspace),
            ]
            self.assertTrue(all(check['status'] == 'passed' for check in checks), checks)
            summary_path = workspace / 'workflow-summary.md'
            summary_path.write_text('# GA real wait barrier\n\nAll dependency, artifact, and compile checks passed.\n', encoding='utf-8')
            metrics['verificationEvidence'] = {check['checkId']: check['status'] for check in checks}
            metrics['hostSummaryArtifact'] = summary_path.name
            tool_calls = []
            transcript_events = []
            for job in loaded.jobs:
                result = _load(Path(loaded.artifact_dir) / (job.result_ref or f'agents/{job.job_id}/result.json'))
                ref = (job.metadata or {}).get('transcriptRef') or result.get('transcriptRef')
                if ref and (Path(loaded.artifact_dir) / ref).exists():
                    for line in (Path(loaded.artifact_dir) / ref).read_text(encoding='utf-8', errors='replace').splitlines():
                        event = _load_line(line)
                        transcript_events.append(event)
                        if event.get('type') == 'tool_call' and event.get('toolName'):
                            tool_calls.append(event['toolName'])
            metrics['toolCalls'] = tool_calls
            metrics['mcpDurationMs'] = _mcp_duration_ms(transcript_events)
            metrics['mcpCalled'] = 'mcp__tavily__tavily_search' in tool_calls
            if MCP_OPT_IN:
                self.assertTrue(metrics['mcpCalled'], metrics)
            metrics['passed'] = True
            report = root / 'metrics.json'
            report.write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding='utf-8')
            print(json.dumps({'passed': True, 'report': report.name, 'metrics': metrics}, ensure_ascii=False))
        except Exception as exc:
            metrics['passed'] = False
            metrics['failureCategories'].append('scheduler_barrier' if 'downstream' in str(exc) else 'workflow_runtime')
            (root / 'metrics.json').write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding='utf-8')
            raise


if __name__ == '__main__':
    unittest.main(verbosity=2)
