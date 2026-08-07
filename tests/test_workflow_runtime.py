import hashlib
import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from workflow_child_agent import AgentResult, FakeChildAgentRunner, NativeGPTChildAgentRunner
from workflow_models import WorkflowJob, WorkflowRun
from workflow_runtime import WorkflowRuntime
from workflow_scheduler import AgentScheduler, SchedulerConfig
from workflow_store import WorkflowStore


class NeverFinishesRunner:
    def __init__(self):
        self.started_job_ids = []
        self.cancelled_job_ids = set()

    def start(self, job):
        self.started_job_ids.append(job.job_id)

    def poll(self, job):
        return None

    def cancel(self, job):
        self.cancelled_job_ids.add(job.job_id)


class FailingAndHangingRunner:
    def __init__(self):
        self.cancelled_job_ids = set()
        self.started_job_ids = []

    def start(self, job):
        self.started_job_ids.append(job.job_id)

    def poll(self, job):
        if job.job_id == "agent_1":
            return AgentResult(job_id=job.job_id, status="failed", payload={"error": "agent one failed"})
        return None

    def cancel(self, job):
        self.cancelled_job_ids.add(job.job_id)


class SucceedsThenFailsRunner:
    def __init__(self):
        self.started_job_ids = []
        self.completed_success = False
        self.cancelled_job_ids = set()

    def start(self, job):
        self.started_job_ids.append(job.job_id)

    def poll(self, job):
        if job.job_id == "agent_1" and not self.completed_success:
            self.completed_success = True
            return AgentResult(
                job_id=job.job_id,
                status="succeeded",
                payload={"summary": "agent one succeeded"},
                transcript_events=[{"type": "assistant", "text": "agent one transcript"}],
            )
        if job.job_id == "agent_2" and self.completed_success:
            return AgentResult(
                job_id=job.job_id,
                status="failed",
                payload={"error": "agent two failed after one success"},
                transcript_events=[{"type": "error", "error": "agent two failed after one success"}],
            )
        return None

    def cancel(self, job):
        self.cancelled_job_ids.add(job.job_id)


class RateLimitAfterFirstSuccessRunner(SucceedsThenFailsRunner):
    error = "HTTP 429 Too Many Requests: provider rate limit exceeded"

    def poll(self, job):
        if job.job_id == "agent_1" and not self.completed_success:
            self.completed_success = True
            return AgentResult(
                job_id=job.job_id,
                status="succeeded",
                payload={"summary": "agent one succeeded before rate limit"},
                transcript_events=[{"type": "assistant", "text": "agent one transcript before rate limit"}],
            )
        if job.job_id == "agent_2" and self.completed_success:
            return AgentResult(
                job_id=job.job_id,
                status="failed",
                payload={"error": self.error, "statusCode": 429, "category": "rate_limit"},
                transcript_events=[{"type": "error", "error": self.error, "statusCode": 429}],
            )
        return None


class CountingRunner(FakeChildAgentRunner):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.started_job_ids = []

    def start(self, job):
        self.started_job_ids.append(job.job_id)
        super().start(job)


class WorkspaceRecordingRunner(FakeChildAgentRunner):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.started_jobs = []

    def start(self, job):
        self.started_jobs.append(job)
        super().start(job)


class FakeStream:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


class FakeProcessForTerminate:
    def __init__(self):
        self.stdin = FakeStream()
        self.stdout = FakeStream()
        self.stderr = FakeStream()
        self.terminated = False
        self.killed = False
        self.wait_calls = 0

    def poll(self):
        return None if not self.killed else -9

    def terminate(self):
        self.terminated = True

    def wait(self, timeout=None):
        self.wait_calls += 1
        if self.wait_calls == 1:
            import subprocess

            raise subprocess.TimeoutExpired(cmd="node", timeout=timeout)
        return -9

    def kill(self):
        self.killed = True


class WorkflowRuntimeTest(unittest.TestCase):
    def large_transcript_events(self, marker, count=1001):
        return [
            {"type": "assistant", "index": index, "text": f"{marker}-event-{index:04d}"}
            for index in range(count)
        ]

    def jsonl_line_count(self, path):
        return sum(1 for line in Path(path).read_text(encoding="utf-8").splitlines() if line.strip())

    def sha256_file(self, path):
        digest = hashlib.sha256()
        with Path(path).open("rb") as fh:
            for chunk in iter(lambda: fh.read(1024 * 64), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def assert_json_file_omits_marker(self, path, marker):
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        text = json.dumps(data, ensure_ascii=False)
        self.assertNotIn("transcriptEvents", text)
        self.assertNotIn(marker, text)

    def assert_runtime_failed_with_marker(self, *, store, run, marker, expected_jobs=0):
        loaded = store.load_run(run.run_id)
        self.assertEqual("failed", loaded.status)
        self.assertEqual("final-result.json", loaded.result_ref)
        self.assertIn(marker, loaded.error)
        self.assertEqual(expected_jobs, len(loaded.jobs))
        events = store.replay_events(run.run_id)
        self.assertEqual("workflow_failed", events[-1].event_type)
        self.assertIn(marker, events[-1].payload["error"])
        final_result = json.loads((Path(run.artifact_dir) / "final-result.json").read_text(encoding="utf-8"))
        self.assertEqual("failed", final_result["status"])
        self.assertIn(marker, final_result["error"])
        self.assertEqual(expected_jobs, len(final_result["jobs"]))
        self.assertEqual("workflow-progress.json", final_result["workflowProgressRef"])
        self.assertTrue((Path(run.artifact_dir) / "workflow-progress.json").exists())
        return loaded, final_result

    def test_runtime_passes_workspace_args_to_child_job_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            script = """
const result = await agent('write a relative file', {label: 'coder'})
return {summary: result.summary}
"""
            run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script=script, status="running"))
            runner = WorkspaceRecordingRunner()

            outcome = WorkflowRuntime(store=store, runner=runner, timeout_seconds=5.0).run(
                run,
                args={"workspacePath": str(workspace)},
            )

            expected = str(workspace.resolve())
            self.assertEqual("completed agent_1", outcome.result["summary"])
            self.assertEqual(expected, runner.started_jobs[0].metadata["workspacePath"])
            loaded = store.load_run(run.run_id)
            self.assertEqual(expected, loaded.metadata["workspacePath"])
            self.assertEqual(expected, loaded.jobs[0].metadata["workspacePath"])

    def test_runtime_rejects_missing_workspace_directory_before_child_start(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            missing_workspace = Path(tmp) / "missing-workspace"
            script = "return await agent('this must not start')"
            run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script=script, status="running"))
            runner = WorkspaceRecordingRunner()

            with self.assertRaisesRegex(ValueError, "workspace must be an existing directory"):
                WorkflowRuntime(store=store, runner=runner, timeout_seconds=5.0).run(
                    run,
                    args={"workspacePath": str(missing_workspace)},
                )

            self.assertEqual([], runner.started_jobs)

    def test_runtime_large_child_transcripts_stay_out_of_result_and_journal(self):
        class LargeTranscriptRunner:
            def __init__(self, test_case):
                self.test_case = test_case
                self.started_job_ids = []

            def start(self, job):
                self.started_job_ids.append(job.job_id)

            def poll(self, job):
                marker = f"GA_P8_LARGE_TRANSCRIPT_{job.job_id}"
                return AgentResult(
                    job_id=job.job_id,
                    status="succeeded",
                    payload={"summary": f"summary {job.job_id}"},
                    transcript_events=self.test_case.large_transcript_events(marker),
                )

            def cancel(self, job):
                return None

        markers = [f"GA_P8_LARGE_TRANSCRIPT_agent_{index}" for index in range(1, 4)]
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            script = """
const results = await parallel([() => agent('large one'), () => agent('large two'), () => agent('large three')])
return results.map(result => result.summary)
"""
            runner = LargeTranscriptRunner(self)
            run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script=script, status="running"))
            outcome = WorkflowRuntime(store=store, runner=runner, scheduler_config=SchedulerConfig(max_concurrent=3, max_total=3)).run(run)

            self.assertEqual(["summary agent_1", "summary agent_2", "summary agent_3"], outcome.result)
            self.assertEqual(["agent_1", "agent_2", "agent_3"], runner.started_job_ids)
            loaded = store.load_run("wf_test")
            artifact_dir = Path(loaded.artifact_dir)
            final_path = artifact_dir / "final-result.json"
            journal_text = (artifact_dir / "journal.jsonl").read_text(encoding="utf-8")
            state_text = json.dumps(loaded.to_dict(), ensure_ascii=False)
            self.assertNotIn("transcriptEvents", journal_text)
            for marker in markers:
                self.assertNotIn(marker, json.dumps(outcome.result, ensure_ascii=False))
                self.assertNotIn(marker, state_text)
                self.assertNotIn(marker, journal_text)
                self.assert_json_file_omits_marker(final_path, marker)

            for job in loaded.jobs:
                marker = f"GA_P8_LARGE_TRANSCRIPT_{job.job_id}"
                transcript_path = artifact_dir / job.metadata["transcriptRef"]
                self.assertTrue(transcript_path.exists())
                self.assertEqual(1001, self.jsonl_line_count(transcript_path))
                self.assertIn(marker, transcript_path.read_text(encoding="utf-8"))
                self.assert_json_file_omits_marker(artifact_dir / job.result_ref, marker)

    def test_runtime_resume_copies_large_transcript_to_resumed_artifact(self):
        marker = "GA_P8_SOURCE_LARGE_TRANSCRIPT_AGENT_1"

        class LargeTranscriptRunner:
            def __init__(self, test_case):
                self.test_case = test_case
                self.started_job_ids = []

            def start(self, job):
                self.started_job_ids.append(job.job_id)

            def poll(self, job):
                return AgentResult(
                    job_id=job.job_id,
                    status="succeeded",
                    payload={"summary": "source summary"},
                    transcript_events=self.test_case.large_transcript_events(marker),
                )

            def cancel(self, job):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            script = """
const result = await agent('large cached')
return result.summary
"""
            source = store.create_run(WorkflowRun(run_id="wf_source", session_id="session_test", script=script, status="running"))
            source_runner = LargeTranscriptRunner(self)
            WorkflowRuntime(store=store, runner=source_runner).run(source, args={"same": True})
            source_loaded = store.load_run("wf_source")
            source_job = source_loaded.jobs[0]
            source_artifact_dir = Path(source_loaded.artifact_dir)
            source_transcript_path = source_artifact_dir / source_job.metadata["transcriptRef"]
            source_hash = self.sha256_file(source_transcript_path)

            resumed = store.create_run(WorkflowRun(run_id="wf_resumed", session_id="session_test", script=script, status="running"))
            runner = CountingRunner(results={"agent_1": {"summary": "fresh should not start"}})
            outcome = WorkflowRuntime(store=store, runner=runner).run(
                resumed,
                args={"same": True},
                resume_from_run_id="wf_source",
            )

            self.assertEqual("source summary", outcome.result)
            self.assertEqual([], runner.started_job_ids)
            resumed_loaded = store.load_run("wf_resumed")
            resumed_artifact_dir = Path(resumed_loaded.artifact_dir)
            cached_job = resumed_loaded.jobs[0]
            self.assertEqual("cached", cached_job.status)
            self.assertEqual("wf_source", cached_job.metadata["cachedFromRunId"])
            self.assertEqual("agent_1", cached_job.metadata["cachedFromJobId"])
            resumed_transcript_path = resumed_artifact_dir / cached_job.metadata["transcriptRef"]
            self.assertTrue(resumed_transcript_path.exists())
            self.assertNotEqual(source_transcript_path, resumed_transcript_path)
            self.assertEqual(1001, self.jsonl_line_count(resumed_transcript_path))
            self.assertEqual(source_hash, self.sha256_file(resumed_transcript_path))
            self.assertIn(marker, resumed_transcript_path.read_text(encoding="utf-8"))
            self.assert_json_file_omits_marker(resumed_artifact_dir / cached_job.result_ref, marker)
            self.assert_json_file_omits_marker(resumed_artifact_dir / "final-result.json", marker)
            journal_text = (resumed_artifact_dir / "journal.jsonl").read_text(encoding="utf-8")
            self.assertNotIn("transcriptEvents", journal_text)
            self.assertNotIn(marker, journal_text)

    def test_runtime_executes_phase_log_and_agent_script_with_fake_runner(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            script = """
export const meta = { name: 'smoke', description: 'smoke' }
phase('Scout')
log('starting')
const result = await agent('inspect repo', { label: 'Scout' })
return { summary: result.summary, phaseDone: true }
"""
            run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script=script, status="running"))
            runtime = WorkflowRuntime(store=store, runner=FakeChildAgentRunner(results={"agent_1": {"summary": "ok"}}))

            outcome = runtime.run(run)

            self.assertEqual({"summary": "ok", "phaseDone": True}, outcome.result)
            self.assertEqual("succeeded", outcome.run.status)
            loaded = store.load_run("wf_test")
            self.assertEqual("succeeded", loaded.status)
            self.assertEqual("final-result.json", loaded.result_ref)
            events = store.replay_events("wf_test")
            self.assertEqual(
                ["workflow_phase", "workflow_log", "agent_registered", "agent_started", "agent_completed"],
                [event.event_type for event in events],
            )
            final_result = json.loads((Path(run.artifact_dir) / "final-result.json").read_text(encoding="utf-8"))
            self.assertEqual("succeeded", final_result["status"])
            self.assertEqual("workflow-progress.json", final_result["workflowProgressRef"])
            self.assertTrue((Path(run.artifact_dir) / "workflow-progress.json").exists())
            self.assertEqual("ok", final_result["result"]["summary"])
            self.assertEqual("agent_1", final_result["jobs"][0]["jobId"])

    def test_runtime_redacts_sensitive_workflow_log_in_memory_and_journal(self):
        secret_values = ["sk-log-secret", "xkey-log-secret", "cookie_secret"]
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            script = """
log('HTTP 200 Authorization: Bearer sk-log-secret; x-api-key: xkey-log-secret; Cookie: sid=cookie_secret; request_id=req_123')
return { ok: true }
"""
            run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script=script, status="running"))
            runtime = WorkflowRuntime(store=store, runner=FakeChildAgentRunner())

            outcome = runtime.run(run)

            loaded = store.load_run("wf_test")
            artifact_dir = Path(loaded.artifact_dir)
            journal_text = (artifact_dir / "journal.jsonl").read_text(encoding="utf-8")
            combined = json.dumps(outcome.logs, ensure_ascii=False) + journal_text
            self.assertIn("request_id=req_123", combined)
            self.assertIn("[REDACTED]", combined)
            for secret in secret_values:
                self.assertNotIn(secret, combined)

    def test_runtime_redacts_success_final_result_in_memory_state_and_final_artifact(self):
        secret_values = ["key_secret", "client_secret", "sk-message-secret", "cookie_secret"]
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            script = """
return {
  apiKey: 'key_secret',
  nested: { clientSecret: 'client_secret' },
  message: 'Bearer sk-message-secret Cookie: sid=cookie_secret request_id=req_123'
}
"""
            run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script=script, status="running"))
            runtime = WorkflowRuntime(store=store, runner=FakeChildAgentRunner())

            outcome = runtime.run(run)

            loaded = store.load_run("wf_test")
            artifact_dir = Path(loaded.artifact_dir)
            final_result = json.loads((artifact_dir / "final-result.json").read_text(encoding="utf-8"))
            combined = json.dumps(outcome.result, ensure_ascii=False) + json.dumps(final_result, ensure_ascii=False)
            self.assertEqual("[REDACTED]", outcome.result["apiKey"])
            self.assertEqual("[REDACTED]", outcome.result["nested"]["clientSecret"])
            self.assertIn("request_id=req_123", combined)
            self.assertIn("[REDACTED]", combined)
            for secret in secret_values:
                self.assertNotIn(secret, combined)

    def test_runtime_worker_error_redacts_secrets_across_error_artifacts(self):
        secret_values = ["sk-worker-secret", "tok_secret", "cookie_secret"]
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            script = """
throw new Error('HTTP 500 Authorization: Bearer sk-worker-secret; token=tok_secret; Cookie: sid=cookie_secret; request_id=req_123')
"""
            run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script=script, status="running"))
            runtime = WorkflowRuntime(store=store, runner=FakeChildAgentRunner())

            with self.assertRaisesRegex(RuntimeError, "HTTP 500") as raised:
                runtime.run(run)

            loaded = store.load_run("wf_test")
            artifact_dir = Path(loaded.artifact_dir)
            final_result = json.loads((artifact_dir / "final-result.json").read_text(encoding="utf-8"))
            journal_text = (artifact_dir / "journal.jsonl").read_text(encoding="utf-8")
            combined = str(raised.exception) + (loaded.error or "") + json.dumps(final_result, ensure_ascii=False) + journal_text
            self.assertIn("HTTP 500", combined)
            self.assertIn("request_id=req_123", combined)
            self.assertIn("[REDACTED]", combined)
            for secret in secret_values:
                self.assertNotIn(secret, combined)

    def test_runtime_worker_sandbox_hides_script_capabilities(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            script = """
return {
  processType: typeof process,
  requireType: typeof require,
  fetchType: typeof fetch,
  websocketType: typeof WebSocket,
}
"""
            run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script=script, status="running"))
            runtime = WorkflowRuntime(store=store, runner=FakeChildAgentRunner())

            outcome = runtime.run(run)

            self.assertEqual(
                {
                    "processType": "undefined",
                    "requireType": "undefined",
                    "fetchType": "undefined",
                    "websocketType": "undefined",
                },
                outcome.result,
            )
            self.assertEqual("succeeded", store.load_run("wf_test").status)

    def test_runtime_allows_script_words_inside_agent_prompt(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            script = """
const result = await agent('Please process this ordinary prompt and explain import and fetch semantics.')
return {summary: result.summary}
"""
            run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script=script, status="running"))
            runtime = WorkflowRuntime(
                store=store,
                runner=FakeChildAgentRunner(results={"agent_1": {"summary": "accepted"}}),
            )

            outcome = runtime.run(run)

            self.assertEqual({"summary": "accepted"}, outcome.result)
            self.assertEqual("succeeded", store.load_run("wf_test").status)

    def test_runtime_marks_run_failed_when_worker_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script="throw new Error('boom')", status="running"))
            runtime = WorkflowRuntime(store=store, runner=FakeChildAgentRunner())

            with self.assertRaises(RuntimeError):
                runtime.run(run)

            self.assert_runtime_failed_with_marker(store=store, run=run, marker="boom")

    def test_runtime_required_python_unittest_gate_blocks_success_when_tests_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            (workspace / "test_gate_failure.py").write_text(
                "import unittest\n\n"
                "class GateFailureTest(unittest.TestCase):\n"
                "    def test_failure(self):\n"
                "        self.fail('GA_P0_TEST_GATE_FAILURE')\n",
                encoding="utf-8",
            )
            script = """
const gate = await runPythonUnittest(args.workspacePath, {pattern: 'test_*.py', timeoutMs: 5000})
return {verificationPassed: gate.passed, gatePassed: gate.gatePassed}
"""
            run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script=script, status="running"))
            runtime = WorkflowRuntime(store=store, runner=FakeChildAgentRunner(), timeout_seconds=5.0)

            with self.assertRaisesRegex(RuntimeError, "test gate failed"):
                runtime.run(run, args={"workspacePath": str(workspace)})

            loaded = store.load_run("wf_test")
            self.assertEqual("failed", loaded.status)
            self.assertIn("GA_P0_TEST_GATE_FAILURE", loaded.error or "")
            artifact_dir = Path(loaded.artifact_dir)
            gate_result_path = artifact_dir / "test-gates" / "gate-1.json"
            self.assertTrue(gate_result_path.exists())
            gate_result = json.loads(gate_result_path.read_text(encoding="utf-8"))
            self.assertFalse(gate_result["passed"])
            self.assertFalse(gate_result["gatePassed"])
            self.assertIn("GA_P0_TEST_GATE_FAILURE", gate_result["stderr"] + gate_result["stdout"])
            self.assertTrue((artifact_dir / "TEST_FAILURES.txt").exists())
            event_types = [event.event_type for event in store.replay_events("wf_test")]
            self.assertIn("workflow_test_gate_started", event_types)
            self.assertIn("workflow_test_gate_completed", event_types)
            self.assertEqual("workflow_failed", event_types[-1])

    def test_runtime_python_unittest_gate_allows_success_when_tests_pass(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            (workspace / "test_gate_success.py").write_text(
                "import unittest\n\n"
                "class GateSuccessTest(unittest.TestCase):\n"
                "    def test_success(self):\n"
                "        self.assertEqual(2 + 2, 4)\n",
                encoding="utf-8",
            )
            script = """
const gate = await runPythonUnittest({workspacePath: args.workspacePath, pattern: 'test_*.py', phase: 'green'})
return {passed: gate.passed, gatePassed: gate.gatePassed, artifactRef: gate.artifactRef}
"""
            run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script=script, status="running"))

            outcome = WorkflowRuntime(store=store, runner=FakeChildAgentRunner(), timeout_seconds=5.0).run(
                run,
                args={"workspacePath": str(workspace)},
            )

            self.assertTrue(outcome.result["passed"])
            self.assertTrue(outcome.result["gatePassed"])
            self.assertEqual("succeeded", store.load_run("wf_test").status)
            self.assertTrue((Path(run.artifact_dir) / "test-gates" / "gate-1.json").exists())
            event_types = [event.event_type for event in store.replay_events("wf_test")]
            self.assertIn("workflow_test_gate_started", event_types)
            self.assertIn("workflow_test_gate_completed", event_types)
            self.assertNotIn("workflow_test_gate_failed", event_types)

    def test_runtime_python_unittest_red_gate_accepts_expected_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            (workspace / "test_gate_red.py").write_text(
                "import unittest\n\n"
                "class GateRedTest(unittest.TestCase):\n"
                "    def test_red(self):\n"
                "        self.fail('GA_P0_EXPECTED_RED')\n",
                encoding="utf-8",
            )
            script = """
const gate = await runPythonUnittest(args.workspacePath, {pattern: 'test_*.py', expect: 'fail', phase: 'red'})
return {passed: gate.passed, gatePassed: gate.gatePassed, expectation: gate.expectation}
"""
            run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script=script, status="running"))

            outcome = WorkflowRuntime(store=store, runner=FakeChildAgentRunner(), timeout_seconds=5.0).run(
                run,
                args={"workspacePath": str(workspace)},
            )

            self.assertFalse(outcome.result["passed"])
            self.assertTrue(outcome.result["gatePassed"])
            self.assertEqual("fail", outcome.result["expectation"])
            self.assertEqual("succeeded", store.load_run("wf_test").status)
            self.assertTrue((Path(run.artifact_dir) / "TEST_FAILURES.txt").exists())
            self.assertNotIn(
                "workflow_test_gate_failed",
                [event.event_type for event in store.replay_events("wf_test")],
            )

    def test_runtime_python_unittest_gate_rejects_workspace_outside_trusted_args(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            workspace = Path(tmp) / "workspace"
            outside = Path(tmp) / "outside"
            workspace.mkdir()
            outside.mkdir()
            script = """
const gate = await runPythonUnittest(args.outsidePath, {pattern: 'test_*.py', expect: 'fail'})
return {passed: gate.passed, gatePassed: gate.gatePassed}
"""
            run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script=script, status="running"))

            with self.assertRaisesRegex(RuntimeError, "outside the allowed workflow workspace"):
                WorkflowRuntime(store=store, runner=FakeChildAgentRunner(), timeout_seconds=5.0).run(
                    run,
                    args={"workspacePath": str(workspace), "outsidePath": str(outside)},
                )

            gate_result = json.loads(
                (Path(run.artifact_dir) / "test-gates" / "gate-1.json").read_text(encoding="utf-8")
            )
            self.assertFalse(gate_result["gatePassed"])
            self.assertIn("outside the allowed workflow workspace", gate_result["error"])

    def test_runtime_python_unittest_gate_records_timeout(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            (workspace / "test_gate_timeout.py").write_text(
                "import time\nimport unittest\n\n"
                "class GateTimeoutTest(unittest.TestCase):\n"
                "    def test_timeout(self):\n"
                "        time.sleep(1.0)\n",
                encoding="utf-8",
            )
            script = """
const gate = await runPythonUnittest(args.workspacePath, {pattern: 'test_*.py', timeoutMs: 50})
return gate
"""
            run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script=script, status="running"))

            with self.assertRaisesRegex(RuntimeError, "timed out"):
                WorkflowRuntime(store=store, runner=FakeChildAgentRunner(), timeout_seconds=5.0).run(
                    run,
                    args={"workspacePath": str(workspace)},
                )

            gate_result = json.loads(
                (Path(run.artifact_dir) / "test-gates" / "gate-1.json").read_text(encoding="utf-8")
            )
            self.assertTrue(gate_result["timedOut"])
            self.assertFalse(gate_result["passed"])

    def test_runtime_python_unittest_gate_truncates_output(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            (workspace / "test_gate_output.py").write_text(
                "import unittest\n\n"
                "class GateOutputTest(unittest.TestCase):\n"
                "    def test_output(self):\n"
                "        print('GA_P0_OUTPUT_' + ('x' * 20000))\n"
                "        self.fail('GA_P0_OUTPUT_FAILURE')\n",
                encoding="utf-8",
            )
            script = """
const gate = await runPythonUnittest(args.workspacePath, {pattern: 'test_*.py'})
return gate
"""
            run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script=script, status="running"))

            with self.assertRaisesRegex(RuntimeError, "test gate failed"):
                WorkflowRuntime(store=store, runner=FakeChildAgentRunner(), timeout_seconds=5.0).run(
                    run,
                    args={"workspacePath": str(workspace)},
                )

            gate_result = json.loads(
                (Path(run.artifact_dir) / "test-gates" / "gate-1.json").read_text(encoding="utf-8")
            )
            self.assertTrue(gate_result["truncated"])
            self.assertLessEqual(len(gate_result["stdout"]), 12_040)
            self.assertIn("GA_P0_OUTPUT_", gate_result["stdout"])
            self.assertIn("[output truncated]", gate_result["stdout"])

    def test_runtime_rejects_explicit_failed_verification_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            script = """
const verification = await agent('return the verification result')
return {verificationPassed: verification.verificationPassed}
"""
            run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script=script, status="running"))
            runner = FakeChildAgentRunner(results={"agent_1": {"verificationPassed": False}})

            with self.assertRaisesRegex(RuntimeError, "verification failed"):
                WorkflowRuntime(store=store, runner=runner, timeout_seconds=5.0).run(run)

            loaded = store.load_run("wf_test")
            self.assertEqual("failed", loaded.status)
            final_result = json.loads((Path(loaded.artifact_dir) / "final-result.json").read_text(encoding="utf-8"))
            self.assertEqual("failed", final_result["status"])
            self.assertFalse(final_result["result"]["verificationPassed"])

    def test_runtime_repair_and_retest_reuses_gate_until_repaired(self):
        class RepairingRunner:
            def __init__(self, test_path):
                self.test_path = Path(test_path)
                self.started_job_ids = []
                self.repaired = False

            def start(self, job):
                self.started_job_ids.append(job.job_id)

            def poll(self, job):
                if not self.repaired:
                    failure_log = self.test_path.parent / "TEST_FAILURES.txt"
                    if not failure_log.exists() or "GA_P0_REPAIR_REQUIRED" not in failure_log.read_text(encoding="utf-8"):
                        raise AssertionError("repair child could not read workspace TEST_FAILURES.txt")
                    self.test_path.write_text(
                        "import unittest\n\n"
                        "class RepairableTest(unittest.TestCase):\n"
                        "    def test_repaired(self):\n"
                        "        self.assertTrue(True)\n",
                        encoding="utf-8",
                    )
                    self.repaired = True
                return AgentResult(job_id=job.job_id, payload={"summary": "repaired"})

            def cancel(self, job):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            test_path = workspace / "test_repairable.py"
            test_path.write_text(
                "import unittest\n\n"
                "class RepairableTest(unittest.TestCase):\n"
                "    def test_repaired(self):\n"
                "        self.fail('GA_P0_REPAIR_REQUIRED')\n",
                encoding="utf-8",
            )
            script = """
const repair = await repairAndRetest({
  workspacePath: args.workspacePath,
  pattern: 'test_*.py',
  maxAttempts: 1,
  repairPrompt: 'Read TEST_FAILURES.txt and repair the failing test.',
  labelPrefix: 'repair'
})
return {passed: repair.passed, gatePassed: repair.gatePassed, repairAttempts: repair.repairAttempts}
"""
            run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script=script, status="running"))
            runner = RepairingRunner(test_path)

            outcome = WorkflowRuntime(store=store, runner=runner, timeout_seconds=5.0).run(
                run,
                args={"workspacePath": str(workspace)},
            )

            self.assertTrue(outcome.result["passed"])
            self.assertTrue(outcome.result["gatePassed"])
            self.assertEqual(1, outcome.result["repairAttempts"])
            self.assertEqual("succeeded", store.load_run("wf_test").status)
            self.assertTrue((Path(run.artifact_dir) / "test-gates" / "gate-1.json").exists())
            self.assertTrue((Path(run.artifact_dir) / "test-gates" / "gate-2.json").exists())
            self.assertEqual(["agent_1"], runner.started_job_ids)
            self.assertEqual("repair-1", store.load_run("wf_test").jobs[0].metadata["label"])

    def test_runtime_repair_and_retest_stops_after_bounded_attempts(self):
        class NonRepairingRunner:
            def __init__(self):
                self.started_job_ids = []

            def start(self, job):
                self.started_job_ids.append(job.job_id)

            def poll(self, job):
                return AgentResult(job_id=job.job_id, payload={"summary": "unable to repair"})

            def cancel(self, job):
                return None

        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            (workspace / "test_unrepairable.py").write_text(
                "import unittest\n\n"
                "class UnrepairableTest(unittest.TestCase):\n"
                "    def test_failure(self):\n"
                "        self.fail('GA_P0_REPAIR_EXHAUSTED')\n",
                encoding="utf-8",
            )
            script = """
const repair = await repairAndRetest({
  workspacePath: args.workspacePath,
  pattern: 'test_*.py',
  maxAttempts: 2,
  repairPrompt: 'Read TEST_FAILURES.txt and repair the failing test.'
})
return repair
"""
            run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script=script, status="running"))
            runner = NonRepairingRunner()

            with self.assertRaisesRegex(RuntimeError, "test gate failed"):
                WorkflowRuntime(store=store, runner=runner, timeout_seconds=5.0).run(
                    run,
                    args={"workspacePath": str(workspace)},
                )

            loaded = store.load_run("wf_test")
            self.assertEqual("failed", loaded.status)
            self.assertEqual(["agent_1", "agent_2"], runner.started_job_ids)
            self.assertEqual(["repair-1", "repair-2"], [job.metadata["label"] for job in loaded.jobs])
            self.assertTrue((Path(run.artifact_dir) / "test-gates" / "gate-3.json").exists())
            final_result = json.loads((Path(run.artifact_dir) / "final-result.json").read_text(encoding="utf-8"))
            self.assertEqual(2, final_result["result"]["repairAttempts"])

    def test_runtime_parallel_thunk_sync_throw_fails_without_agent_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            script = """
const results = await parallel([
  () => 1,
  () => { throw new Error('GA_P8_PARALLEL_THUNK_THROW') }
])
return results
"""
            run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script=script, status="running"))
            runtime = WorkflowRuntime(store=store, runner=FakeChildAgentRunner())

            with self.assertRaisesRegex(RuntimeError, "GA_P8_PARALLEL_THUNK_THROW"):
                runtime.run(run)

            self.assert_runtime_failed_with_marker(store=store, run=run, marker="GA_P8_PARALLEL_THUNK_THROW")

    def test_runtime_marks_bigint_return_as_failed_worker_serialization_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script="return 1n", status="running"))
            runtime = WorkflowRuntime(store=store, runner=FakeChildAgentRunner())

            with self.assertRaisesRegex(RuntimeError, "BigInt"):
                runtime.run(run)

            self.assert_runtime_failed_with_marker(store=store, run=run, marker="BigInt")

    def test_runtime_marks_circular_return_as_failed_worker_serialization_error(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            script = """
const value = {}
value.self = value
return value
"""
            run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script=script, status="running"))
            runtime = WorkflowRuntime(store=store, runner=FakeChildAgentRunner())

            with self.assertRaisesRegex(RuntimeError, "circular"):
                runtime.run(run)

            self.assert_runtime_failed_with_marker(store=store, run=run, marker="circular")

    def test_runtime_parallel_registers_all_agents_before_waiting_for_results(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            script = """
const results = await parallel([() => agent('one'), () => agent('two')])
return results.map(r => r.summary)
"""
            run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script=script, status="running"))
            runtime = WorkflowRuntime(
                store=store,
                runner=FakeChildAgentRunner(delay_ticks=1, results={"agent_1": {"summary": "one"}, "agent_2": {"summary": "two"}}),
                scheduler_config=SchedulerConfig(max_concurrent=2),
            )

            outcome = runtime.run(run)

            self.assertEqual(["one", "two"], outcome.result)
            event_types = [event.event_type for event in store.replay_events("wf_test")]
            first_completed = event_types.index("agent_completed")
            self.assertEqual(2, event_types[:first_completed].count("agent_registered"))
            self.assertEqual(2, event_types[:first_completed].count("agent_started"))

    def test_runtime_parallel_respects_scheduler_max_concurrent(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            script = """
const results = await parallel([() => agent('one'), () => agent('two'), () => agent('three')])
return results.map(r => r.summary)
"""
            run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script=script, status="running"))
            runtime = WorkflowRuntime(
                store=store,
                runner=FakeChildAgentRunner(
                    delay_ticks=2,
                    results={"agent_1": {"summary": "one"}, "agent_2": {"summary": "two"}, "agent_3": {"summary": "three"}},
                ),
                scheduler_config=SchedulerConfig(max_concurrent=2),
            )

            outcome = runtime.run(run)

            self.assertEqual(["one", "two", "three"], outcome.result)
            events = store.replay_events("wf_test")
            event_pairs = [(event.event_type, event.job_id) for event in events]
            start_agent_3 = event_pairs.index(("agent_started", "agent_3"))
            first_completed = next(index for index, pair in enumerate(event_pairs) if pair[0] == "agent_completed")
            self.assertLess(first_completed, start_agent_3)
            self.assertIn(("agent_started", "agent_1"), event_pairs[:first_completed])
            self.assertIn(("agent_started", "agent_2"), event_pairs[:first_completed])

    def test_runtime_pipeline_runs_stage_batches_and_preserves_item_order(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            script = """
const results = await pipeline(
  ['a', 'b'],
  async item => agent('stage1 ' + item),
  async prev => agent('stage2 ' + prev.summary)
)
return results.map(r => r.summary)
"""
            run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script=script, status="running"))
            runtime = WorkflowRuntime(
                store=store,
                runner=FakeChildAgentRunner(
                    delay_ticks=1,
                    results={
                        "agent_1": {"summary": "A"},
                        "agent_2": {"summary": "B"},
                        "agent_3": {"summary": "AA"},
                        "agent_4": {"summary": "BB"},
                    },
                ),
                scheduler_config=SchedulerConfig(max_concurrent=2),
            )

            outcome = runtime.run(run)

            self.assertEqual(["AA", "BB"], outcome.result)
            loaded = store.load_run("wf_test")
            self.assertEqual(4, len(loaded.jobs))
            self.assertEqual(["stage1 a", "stage1 b", "stage2 A", "stage2 B"], [job.prompt for job in loaded.jobs])
            events = store.replay_events("wf_test")
            event_pairs = [(event.event_type, event.job_id) for event in events]
            stage2_first_registered = event_pairs.index(("agent_registered", "agent_3"))
            self.assertLess(event_pairs.index(("agent_completed", "agent_1")), stage2_first_registered)
            self.assertLess(event_pairs.index(("agent_completed", "agent_2")), stage2_first_registered)

    def test_runtime_agent_options_label_is_persisted(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            script = """
const result = await agent('inspect repo', { label: 'Scout' })
return result.summary
"""
            run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script=script, status="running"))
            runtime = WorkflowRuntime(store=store, runner=FakeChildAgentRunner(results={"agent_1": {"summary": "ok"}}))

            runtime.run(run)

            loaded = store.load_run("wf_test")
            self.assertEqual("Scout", loaded.jobs[0].metadata["label"])
            registered = next(event for event in store.replay_events("wf_test") if event.event_type == "agent_registered")
            self.assertEqual("Scout", registered.payload["label"])

    def test_runtime_agent_falsy_options_are_treated_as_empty_options(self):
        scripts = {
            "omitted": "return await agent('p')",
            "undefined": "return await agent('p', undefined)",
            "null": "return await agent('p', null)",
            "false": "return await agent('p', false)",
            "zero": "return await agent('p', 0)",
            "empty-string": "return await agent('p', '')",
        }
        for name, script in scripts.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                store = WorkflowStore(root=tmp)
                run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script=script, status="running"))
                runtime = WorkflowRuntime(store=store, runner=FakeChildAgentRunner(results={"agent_1": {"summary": "ok"}}))

                runtime.run(run)

                loaded = store.load_run("wf_test")
                self.assertEqual("succeeded", loaded.status)
                self.assertEqual(1, len(loaded.jobs))
                self.assertEqual({}, loaded.jobs[0].metadata["options"])
                self.assertIsNone(loaded.jobs[0].metadata["label"])

    def test_runtime_agent_plain_object_options_preserve_label_and_options(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            script = """
const result = await agent('p', {label:'Scout', effort:'low'})
return result.summary
"""
            run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script=script, status="running"))
            runtime = WorkflowRuntime(store=store, runner=FakeChildAgentRunner(results={"agent_1": {"summary": "ok"}}))

            runtime.run(run)

            loaded = store.load_run("wf_test")
            self.assertEqual("succeeded", loaded.status)
            self.assertEqual(1, len(loaded.jobs))
            self.assertEqual("Scout", loaded.jobs[0].metadata["label"])
            self.assertEqual({"label": "Scout", "effort": "low"}, loaded.jobs[0].metadata["options"])
            self.assertIn("cacheKey", loaded.jobs[0].metadata)

    def test_runtime_agent_schema_failure_falls_back_to_text_and_records_workflow_issue(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            script = """
const result = await agent('collect sources', {
  label: 'collector',
  schema: {
    type: 'object',
    required: ['sources'],
    properties: { sources: { type: 'array' } }
  },
  fallback: 'text'
})
return { summary: result.summary, fallback: result.schemaFallback }
"""
            run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script=script, status="running"))
            runtime = WorkflowRuntime(store=store, runner=FakeChildAgentRunner(results={"agent_1": {"summary": "plain text only"}}))

            outcome = runtime.run(run)

            self.assertEqual({"summary": "plain text only", "fallback": True}, outcome.result)
            loaded = store.load_run("wf_test")
            self.assertEqual("succeeded", loaded.status)
            job = loaded.jobs[0]
            self.assertEqual("succeeded", job.status)
            self.assertTrue(job.metadata["schemaValidation"]["fallbackApplied"])
            self.assertEqual("schema_validation_failed", job.metadata["schemaValidation"]["code"])
            self.assertEqual("text", job.metadata["schemaValidation"]["fallback"])
            self.assertTrue(job.metadata["result"]["schemaFallback"])
            issues = loaded.metadata["workflowIssues"]
            self.assertEqual(1, len(issues))
            self.assertEqual("schema_validation_failed", issues[0]["code"])
            self.assertEqual("agent_1", issues[0]["jobId"])
            self.assertEqual("text", issues[0]["fallback"])
            event = next(event for event in store.replay_events("wf_test") if event.event_type == "workflow_issue")
            self.assertEqual("schema_validation_failed", event.payload["code"])
            self.assertEqual("agent_1", event.job_id)
            progress = json.loads((Path(run.artifact_dir) / "workflow-progress.json").read_text(encoding="utf-8"))
            self.assertEqual(issues, progress["workflowIssues"])
            self.assertEqual(job.metadata["schemaValidation"], progress["workflowProgress"][0]["schemaValidation"])

    def test_runtime_agent_schema_failure_without_fallback_fails_with_schema_issue(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            script = """
const result = await agent('collect sources', {
  label: 'collector',
  schema: {
    type: 'object',
    required: ['sources'],
    properties: { sources: { type: 'array' } }
  }
})
return result
"""
            run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script=script, status="running"))
            runtime = WorkflowRuntime(store=store, runner=FakeChildAgentRunner(results={"agent_1": {"summary": "plain text only"}}))

            with self.assertRaisesRegex(RuntimeError, "schema_validation_failed"):
                runtime.run(run)

            loaded = store.load_run("wf_test")
            self.assertEqual("failed", loaded.status)
            self.assertIn("schema_validation_failed", loaded.error)
            job = loaded.jobs[0]
            self.assertEqual("failed", job.status)
            self.assertEqual("schema_validation_failed", job.metadata["schemaValidation"]["code"])
            self.assertFalse(job.metadata["schemaValidation"]["fallbackApplied"])
            issues = loaded.metadata["workflowIssues"]
            self.assertEqual(1, len(issues))
            self.assertEqual("schema_validation_failed", issues[0]["code"])
            event_types = [event.event_type for event in store.replay_events("wf_test")]
            self.assertIn("agent_failed", event_types)
            self.assertIn("workflow_issue", event_types)
            self.assertEqual("workflow_failed", event_types[-1])
            final_result = json.loads((Path(run.artifact_dir) / "final-result.json").read_text(encoding="utf-8"))
            self.assertEqual("failed", final_result["status"])
            self.assertIn("schema_validation_failed", final_result["error"])
            self.assertEqual(issues, final_result["workflowIssues"])

    def test_runtime_agent_truthy_non_object_options_fail_before_registering_job(self):
        scripts = {
            "string": "return await agent('p', 'abc')",
            "number": "return await agent('p', 1)",
            "array": "return await agent('p', ['x'])",
            "array-pairs": "return await agent('p', [['label', 'Scout'], ['effort', 'low']])",
        }
        for name, script in scripts.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                store = WorkflowStore(root=tmp)
                run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script=script, status="running"))
                runtime = WorkflowRuntime(store=store, runner=FakeChildAgentRunner(results={"agent_1": {"summary": "ok"}}))

                with self.assertRaisesRegex(RuntimeError, "agent options must be a plain object"):
                    runtime.run(run)

                self.assert_runtime_failed_with_marker(
                    store=store,
                    run=run,
                    marker="agent options must be a plain object",
                )

    def test_runtime_agent_label_option_must_be_string_when_present(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script="return await agent('p', {label: 123})", status="running"))
            runtime = WorkflowRuntime(store=store, runner=FakeChildAgentRunner(results={"agent_1": {"summary": "ok"}}))

            with self.assertRaisesRegex(Exception, "agent option label must be a string"):
                runtime.run(run)

            self.assert_runtime_failed_with_marker(
                store=store,
                run=run,
                marker="agent option label must be a string",
            )

    def test_runtime_native_runner_empty_content_preserves_artifacts_and_empty_summary(self):
        class EmptySession:
            last_usage_tokens = {}

            def ask(self, message):
                if False:
                    yield "unreachable"
                return

        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script="return await agent('empty content')", status="running"))
            runtime = WorkflowRuntime(store=store, runner=NativeGPTChildAgentRunner(session_factory=lambda _config_name: EmptySession()))

            outcome = runtime.run(run)

            self.assertEqual("", outcome.result["summary"])
            loaded = store.load_run("wf_test")
            self.assertEqual("succeeded", loaded.status)
            self.assertEqual(1, len(loaded.jobs))
            job = loaded.jobs[0]
            artifact_dir = Path(loaded.artifact_dir)
            result_payload = json.loads((artifact_dir / job.result_ref).read_text(encoding="utf-8"))
            self.assertEqual("succeeded", result_payload["status"])
            self.assertEqual("", result_payload["payload"]["summary"])
            self.assertEqual("", result_payload["payload"]["text"])
            self.assertEqual({}, result_payload["tokenUsage"])
            transcript_path = artifact_dir / job.metadata["transcriptRef"]
            self.assertTrue(transcript_path.exists())
            transcript_rows = [json.loads(line) for line in transcript_path.read_text(encoding="utf-8").splitlines()]
            self.assertTrue(any(row.get("type") == "assistant" and row.get("text") == "" for row in transcript_rows))

    def test_runtime_native_runner_sdk_exception_failed_result_preserves_transcript_artifact(self):
        class ErrorSession:
            last_usage_tokens = {}

            def ask(self, message):
                raise RuntimeError("provider sdk exploded")

        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script="return await agent('sdk exception')", status="running"))
            runtime = WorkflowRuntime(store=store, runner=NativeGPTChildAgentRunner(session_factory=lambda _config_name: ErrorSession()))

            with self.assertRaisesRegex(RuntimeError, "provider sdk exploded"):
                runtime.run(run)

            loaded = store.load_run("wf_test")
            self.assertEqual("failed", loaded.status)
            self.assertEqual(1, len(loaded.jobs))
            job = loaded.jobs[0]
            artifact_dir = Path(loaded.artifact_dir)
            result_payload = json.loads((artifact_dir / job.result_ref).read_text(encoding="utf-8"))
            self.assertEqual("failed", result_payload["status"])
            self.assertIn("provider sdk exploded", result_payload["payload"]["error"])
            transcript_path = artifact_dir / job.metadata["transcriptRef"]
            self.assertTrue(transcript_path.exists())
            transcript_rows = [json.loads(line) for line in transcript_path.read_text(encoding="utf-8").splitlines()]
            self.assertTrue(any(row.get("type") == "error" and "provider sdk exploded" in row.get("error", "") for row in transcript_rows))
            final_result = json.loads((artifact_dir / "final-result.json").read_text(encoding="utf-8"))
            self.assertEqual("failed", final_result["status"])
            self.assertIn("provider sdk exploded", final_result["error"])

    def test_runtime_native_runner_sdk_exception_redacts_provider_secrets_across_artifacts(self):
        secret_text = "HTTP 503 upstream; Authorization: Bearer sk-test-secret-1234567890; x-api-key: xkey-test-secret-123; api_key=ga_test_secret_456; request_id=req_123"
        leaked_values = [
            "sk-test-secret-1234567890",
            "xkey-test-secret-123",
            "ga_test_secret_456",
        ]

        class ErrorSession:
            last_usage_tokens = {}

            def ask(self, message):
                raise RuntimeError(secret_text)

        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script="return await agent('sdk exception')", status="running"))
            runtime = WorkflowRuntime(store=store, runner=NativeGPTChildAgentRunner(session_factory=lambda _config_name: ErrorSession()))

            with self.assertRaisesRegex(RuntimeError, "HTTP 503") as raised:
                runtime.run(run)
            raised_text = str(raised.exception)
            self.assertIn("HTTP 503", raised_text)
            self.assertIn("request_id=req_123", raised_text)
            self.assertIn("[REDACTED]", raised_text)
            for leaked in leaked_values:
                self.assertNotIn(leaked, raised_text)

            loaded = store.load_run("wf_test")
            artifact_dir = Path(loaded.artifact_dir)
            job = loaded.jobs[0]
            result_payload = json.loads((artifact_dir / job.result_ref).read_text(encoding="utf-8"))
            transcript_path = artifact_dir / job.metadata["transcriptRef"]
            transcript_text = transcript_path.read_text(encoding="utf-8")
            final_result = json.loads((artifact_dir / "final-result.json").read_text(encoding="utf-8"))
            journal_text = (artifact_dir / "journal.jsonl").read_text(encoding="utf-8")
            combined = json.dumps(loaded.to_dict(), ensure_ascii=False) + json.dumps(result_payload, ensure_ascii=False) + transcript_text + json.dumps(final_result, ensure_ascii=False) + journal_text

            self.assertEqual("failed", loaded.status)
            self.assertIn("HTTP 503", combined)
            self.assertIn("request_id=req_123", combined)
            self.assertIn("[REDACTED]", combined)
            for leaked in leaked_values:
                self.assertNotIn(leaked, combined)
            self.assertTrue(any(event.event_type == "agent_failed" for event in store.replay_events(run.run_id)))
            self.assertEqual("workflow_failed", store.replay_events(run.run_id)[-1].event_type)

    def test_runtime_reuses_cached_agent_when_resuming_same_script_and_args(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            script = """
const result = await agent('inspect repo', { label: 'Scout' })
return { summary: result.summary }
"""
            original = store.create_run(WorkflowRun(run_id="wf_source", session_id="session_test", script=script, status="running"))
            WorkflowRuntime(store=store, runner=CountingRunner(results={"agent_1": {"summary": "cached ok"}})).run(
                original,
                args={"target": "repo"},
            )
            resumed = store.create_run(WorkflowRun(run_id="wf_resumed", session_id="session_test", script=script, status="running"))
            runner = CountingRunner(results={"agent_1": {"summary": "fresh"}})
            runtime = WorkflowRuntime(store=store, runner=runner)

            outcome = runtime.run(resumed, args={"target": "repo"}, resume_from_run_id="wf_source")

            self.assertEqual({"summary": "cached ok"}, outcome.result)
            self.assertEqual([], runner.started_job_ids)
            loaded = store.load_run("wf_resumed")
            self.assertEqual("cached", loaded.jobs[0].status)
            self.assertEqual({"summary": "cached ok"}, loaded.jobs[0].metadata["result"])
            event_types = [event.event_type for event in store.replay_events("wf_resumed")]
            self.assertIn("agent_cached", event_types)
            self.assertNotIn("agent_started", event_types)

    def test_runtime_does_not_reuse_cached_agent_when_resume_args_change(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            script = """
const result = await agent('inspect ' + args.target)
return result.summary
"""
            original = store.create_run(WorkflowRun(run_id="wf_source", session_id="session_test", script=script, status="running"))
            WorkflowRuntime(store=store, runner=CountingRunner(results={"agent_1": {"summary": "old"}})).run(
                original,
                args={"target": "old"},
            )
            resumed = store.create_run(WorkflowRun(run_id="wf_resumed", session_id="session_test", script=script, status="running"))
            runner = CountingRunner(results={"agent_1": {"summary": "new"}})

            outcome = WorkflowRuntime(store=store, runner=runner).run(
                resumed,
                args={"target": "new"},
                resume_from_run_id="wf_source",
            )

            self.assertEqual("new", outcome.result)
            self.assertEqual(["agent_1"], runner.started_job_ids)
            loaded = store.load_run("wf_resumed")
            self.assertEqual("succeeded", loaded.jobs[0].status)
            event_types = [event.event_type for event in store.replay_events("wf_resumed")]
            self.assertNotIn("agent_cached", event_types)
            self.assertIn("agent_started", event_types)

    def test_runtime_does_not_reuse_cached_agent_when_resume_args_change_type_only(self):
        for source_args, resumed_args in (({}, "{}"), (None, "null")):
            with self.subTest(source_args=source_args, resumed_args=resumed_args):
                with tempfile.TemporaryDirectory() as tmp:
                    store = WorkflowStore(root=tmp)
                    script = """
const result = await agent('inspect repo')
return result.summary
"""
                    original = store.create_run(WorkflowRun(run_id="wf_source", session_id="session_test", script=script, status="running"))
                    WorkflowRuntime(store=store, runner=CountingRunner(results={"agent_1": {"summary": "old"}})).run(
                        original,
                        args=source_args,
                    )
                    resumed = store.create_run(WorkflowRun(run_id="wf_resumed", session_id="session_test", script=script, status="running"))
                    runner = CountingRunner(results={"agent_1": {"summary": "fresh"}})

                    outcome = WorkflowRuntime(store=store, runner=runner).run(
                        resumed,
                        args=resumed_args,
                        resume_from_run_id="wf_source",
                    )

                    self.assertEqual("fresh", outcome.result)
                    self.assertEqual(["agent_1"], runner.started_job_ids)
                    event_types = [event.event_type for event in store.replay_events("wf_resumed")]
                    self.assertNotIn("agent_cached", event_types)

    def test_runtime_does_not_reuse_cached_agent_when_permission_profile_or_policy_version_changes(self):
        cases = [
            ("profile", {"permission_profile": "read_only"}),
            ("policy", {"permission_policy_version": "inherit-current-v2"}),
        ]
        for name, overrides in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                store = WorkflowStore(root=tmp)
                script = """
const result = await agent('inspect repo')
return result.summary
"""
                original = store.create_run(WorkflowRun(run_id="wf_source", session_id="session_test", script=script, status="running"))
                WorkflowRuntime(store=store, runner=CountingRunner(results={"agent_1": {"summary": "old"}})).run(
                    original,
                    args={"same": True},
                )
                resumed = store.create_run(
                    WorkflowRun(
                        run_id="wf_resumed",
                        session_id="session_test",
                        script=script,
                        status="running",
                        **overrides,
                    )
                )
                runner = CountingRunner(results={"agent_1": {"summary": "fresh"}})

                outcome = WorkflowRuntime(store=store, runner=runner).run(
                    resumed,
                    args={"same": True},
                    resume_from_run_id="wf_source",
                )

                self.assertEqual("fresh", outcome.result)
                self.assertEqual(["agent_1"], runner.started_job_ids)
                loaded = store.load_run("wf_resumed")
                self.assertEqual("succeeded", loaded.jobs[0].status)
                self.assertNotIn("cachedFromRunId", loaded.jobs[0].metadata)
                self.assertNotIn("cachedFromJobId", loaded.jobs[0].metadata)
                event_types = [event.event_type for event in store.replay_events("wf_resumed")]
                self.assertNotIn("agent_cached", event_types)
                self.assertIn("agent_started", event_types)

    def test_runtime_does_not_reuse_cached_agent_when_tool_or_mcp_context_changes(self):
        cases = [
            (
                "tool-context",
                {"toolContext": {"allowedTools": ["Read"], "toolSchemaHash": "schema-v1"}},
                {"toolContext": {"allowedTools": ["Read", "Write"], "toolSchemaHash": "schema-v1"}},
                "toolContextHash",
            ),
            (
                "mcp-config",
                {"mcpContext": {"configName": "default", "schemaHash": "schema-v1"}},
                {"mcpContext": {"configName": "locked-down", "schemaHash": "schema-v1"}},
                "mcpContextHash",
            ),
            (
                "mcp-schema",
                {"mcpContext": {"configName": "default", "schemaHash": "schema-a"}},
                {"mcpContext": {"configName": "default", "schemaHash": "schema-b"}},
                "mcpContextHash",
            ),
        ]
        for name, source_metadata, resumed_metadata, hash_field in cases:
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                store = WorkflowStore(root=tmp)
                script = """
const result = await agent('inspect repo')
return result.summary
"""
                original = store.create_run(
                    WorkflowRun(
                        run_id="wf_source",
                        session_id="session_test",
                        script=script,
                        status="running",
                        metadata=source_metadata,
                    )
                )
                WorkflowRuntime(store=store, runner=CountingRunner(results={"agent_1": {"summary": "old"}})).run(
                    original,
                    args={"same": True},
                )
                source_job = store.load_run("wf_source").jobs[0]
                resumed = store.create_run(
                    WorkflowRun(
                        run_id="wf_resumed",
                        session_id="session_test",
                        script=script,
                        status="running",
                        metadata=resumed_metadata,
                    )
                )
                runner = CountingRunner(results={"agent_1": {"summary": "fresh"}})

                outcome = WorkflowRuntime(store=store, runner=runner).run(
                    resumed,
                    args={"same": True},
                    resume_from_run_id="wf_source",
                )

                self.assertEqual("fresh", outcome.result)
                self.assertEqual(["agent_1"], runner.started_job_ids)
                loaded = store.load_run("wf_resumed")
                self.assertEqual("succeeded", loaded.jobs[0].status)
                self.assertNotIn("cachedFromRunId", loaded.jobs[0].metadata)
                self.assertNotIn("cachedFromJobId", loaded.jobs[0].metadata)
                self.assertNotEqual(source_job.metadata["cacheKey"][hash_field], loaded.jobs[0].metadata["cacheKey"][hash_field])
                event_types = [event.event_type for event in store.replay_events("wf_resumed")]
                self.assertNotIn("agent_cached", event_types)
                self.assertIn("agent_started", event_types)

    def test_runtime_reuses_cached_agent_when_tool_and_mcp_context_unchanged(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            script = """
const result = await agent('inspect repo')
return result.summary
"""
            source_metadata = {
                "toolContext": {"allowedTools": ["Edit", "Read"], "toolSchemaHash": "schema-v1"},
                "mcpContext": {"configName": "default", "schemaHash": "mcp-schema-v1"},
            }
            resumed_metadata = {
                "toolContext": {"allowedTools": ["Edit", "Read"], "toolSchemaHash": "schema-v1"},
                "mcpContext": {"configName": "default", "schemaHash": "mcp-schema-v1"},
            }
            original = store.create_run(WorkflowRun(run_id="wf_source", session_id="session_test", script=script, status="running", metadata=source_metadata))
            WorkflowRuntime(store=store, runner=CountingRunner(results={"agent_1": {"summary": "cached ok"}})).run(
                original,
                args={"same": True},
            )
            resumed = store.create_run(WorkflowRun(run_id="wf_resumed", session_id="session_test", script=script, status="running", metadata=resumed_metadata))
            runner = CountingRunner(results={"agent_1": {"summary": "fresh"}})

            outcome = WorkflowRuntime(store=store, runner=runner).run(
                resumed,
                args={"same": True},
                resume_from_run_id="wf_source",
            )

            self.assertEqual("cached ok", outcome.result)
            self.assertEqual([], runner.started_job_ids)
            loaded = store.load_run("wf_resumed")
            self.assertEqual("cached", loaded.jobs[0].status)
            self.assertEqual("wf_source", loaded.jobs[0].metadata["cachedFromRunId"])
            self.assertEqual("agent_1", loaded.jobs[0].metadata["cachedFromJobId"])
            event_types = [event.event_type for event in store.replay_events("wf_resumed")]
            self.assertIn("agent_cached", event_types)
            self.assertNotIn("agent_started", event_types)

    def test_runtime_does_not_reuse_cached_agent_across_sessions(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            script = """
const result = await agent('inspect repo')
return result.summary
"""
            original = store.create_run(WorkflowRun(run_id="wf_source", session_id="session_source", script=script, status="running"))
            WorkflowRuntime(store=store, runner=CountingRunner(results={"agent_1": {"summary": "old"}})).run(
                original,
                args={"same": True},
            )
            resumed = store.create_run(WorkflowRun(run_id="wf_resumed", session_id="session_other", script=script, status="running"))
            runner = CountingRunner(results={"agent_1": {"summary": "fresh"}})

            outcome = WorkflowRuntime(store=store, runner=runner).run(
                resumed,
                args={"same": True},
                resume_from_run_id="wf_source",
            )

            self.assertEqual("fresh", outcome.result)
            self.assertEqual(["agent_1"], runner.started_job_ids)
            event_types = [event.event_type for event in store.replay_events("wf_resumed")]
            self.assertNotIn("agent_cached", event_types)

    def test_runtime_cached_agent_transcript_ref_points_to_resumed_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            script = """
const result = await agent('inspect repo')
return result.summary
"""
            original = store.create_run(WorkflowRun(run_id="wf_source", session_id="session_test", script=script, status="running"))
            source_runner = CountingRunner(results={"agent_1": {"summary": "old"}})
            WorkflowRuntime(store=store, runner=source_runner).run(original, args={"same": True})
            source_job = store.load_run("wf_source").jobs[0]
            store.write_agent_transcript(original, source_job, [{"type": "assistant", "text": "source transcript"}])
            source_result = store.read_agent_result(original, source_job)
            source_result.transcript_ref = "agents/agent_1/transcript.jsonl"
            store.write_agent_result(original, source_job, source_result)

            resumed = store.create_run(WorkflowRun(run_id="wf_resumed", session_id="session_test", script=script, status="running"))
            runner = CountingRunner(results={"agent_1": {"summary": "fresh"}})

            outcome = WorkflowRuntime(store=store, runner=runner).run(
                resumed,
                args={"same": True},
                resume_from_run_id="wf_source",
            )

            self.assertEqual("old", outcome.result)
            self.assertEqual([], runner.started_job_ids)
            loaded = store.load_run("wf_resumed")
            cached_job = loaded.jobs[0]
            self.assertEqual("cached", cached_job.status)
            self.assertEqual("agents/agent_1/transcript.jsonl", cached_job.metadata["transcriptRef"])
            transcript_path = Path(loaded.artifact_dir) / cached_job.metadata["transcriptRef"]
            self.assertTrue(transcript_path.exists())
            self.assertEqual([{"type": "assistant", "text": "source transcript"}], [json.loads(line) for line in transcript_path.read_text(encoding="utf-8").splitlines()])

    def test_runtime_reuses_longest_unchanged_agent_prefix_after_script_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            source_script = """
const first = await agent('same first')
const second = await agent('old second')
return [first.summary, second.summary]
"""
            source = store.create_run(WorkflowRun(run_id="wf_source", session_id="session_test", script=source_script, status="running"))
            WorkflowRuntime(
                store=store,
                runner=CountingRunner(results={"agent_1": {"summary": "first cached"}, "agent_2": {"summary": "old"}}),
            ).run(source, args={"same": True})
            resumed_script = """
const first = await agent('same first')
const second = await agent('new second')
return [first.summary, second.summary]
"""
            resumed = store.create_run(WorkflowRun(run_id="wf_resumed", session_id="session_test", script=resumed_script, status="running"))
            runner = CountingRunner(results={"agent_2": {"summary": "new fresh"}})

            outcome = WorkflowRuntime(store=store, runner=runner).run(
                resumed,
                args={"same": True},
                resume_from_run_id="wf_source",
            )

            self.assertEqual(["first cached", "new fresh"], outcome.result)
            self.assertEqual(["agent_2"], runner.started_job_ids)
            loaded = store.load_run("wf_resumed")
            self.assertEqual(["cached", "succeeded"], [job.status for job in loaded.jobs])
            event_types = [event.event_type for event in store.replay_events("wf_resumed")]
            self.assertEqual(1, event_types.count("agent_cached"))
            self.assertEqual(1, event_types.count("agent_started"))

    def test_runtime_reuses_completed_prefix_when_resuming_from_interrupted_source_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            script = """
const first = await agent('first')
const second = await agent('second')
const third = await agent('third')
return [first.summary, second.summary, third.summary]
"""
            source_runner = CountingRunner(
                results={
                    "agent_1": {"summary": "first cached"},
                    "agent_2": {"summary": "second cached"},
                    "agent_3": {"summary": "third stale"},
                }
            )
            source = store.create_run(WorkflowRun(run_id="wf_source", session_id="session_test", script=script, status="running"))
            WorkflowRuntime(store=store, runner=source_runner).run(source, args={"same": True})
            source = store.load_run("wf_source")
            source.status = "interrupted"
            source.jobs[2].status = "stale"
            store.write_agent_transcript(source, source.jobs[0], [{"type": "assistant", "text": "source first"}])
            first_result = store.read_agent_result(source, source.jobs[0])
            first_result.transcript_ref = "agents/agent_1/transcript.jsonl"
            store.write_agent_result(source, source.jobs[0], first_result)
            store.save_run(source)

            resumed = store.create_run(WorkflowRun(run_id="wf_resumed", session_id="session_test", script=script, status="running"))
            runner = CountingRunner(results={"agent_3": {"summary": "third fresh"}})

            outcome = WorkflowRuntime(store=store, runner=runner).run(
                resumed,
                args={"same": True},
                resume_from_run_id="wf_source",
            )

            self.assertEqual(["first cached", "second cached", "third fresh"], outcome.result)
            self.assertEqual(["agent_3"], runner.started_job_ids)
            loaded = store.load_run("wf_resumed")
            self.assertEqual(["cached", "cached", "succeeded"], [job.status for job in loaded.jobs])
            self.assertEqual("wf_source", loaded.jobs[0].metadata["cachedFromRunId"])
            self.assertEqual("agent_1", loaded.jobs[0].metadata["cachedFromJobId"])
            self.assertEqual("agents/agent_1/transcript.jsonl", loaded.jobs[0].metadata["transcriptRef"])
            transcript_path = Path(loaded.artifact_dir) / loaded.jobs[0].metadata["transcriptRef"]
            self.assertTrue(transcript_path.exists())
            self.assertEqual(
                [{"type": "assistant", "text": "source first"}],
                [json.loads(line) for line in transcript_path.read_text(encoding="utf-8").splitlines()],
            )
            self.assertEqual({"summary": "first cached"}, store.read_agent_result(loaded, loaded.jobs[0]).payload)
            self.assertEqual({"summary": "second cached"}, loaded.jobs[1].metadata["result"])
            self.assertEqual("wf_source", loaded.jobs[1].metadata["cachedFromRunId"])
            self.assertEqual("agent_2", loaded.jobs[1].metadata["cachedFromJobId"])
            self.assertEqual({"summary": "second cached"}, store.read_agent_result(loaded, loaded.jobs[1]).payload)
            events = store.replay_events("wf_resumed")
            event_types = [event.event_type for event in events]
            self.assertEqual(2, event_types.count("agent_cached"))
            self.assertEqual(1, event_types.count("agent_started"))
            cached_events = [event for event in events if event.event_type == "agent_cached"]
            self.assertEqual(["agent_1", "agent_2"], [event.payload["sourceJobId"] for event in cached_events])
            self.assertEqual(["wf_source", "wf_source"], [event.payload["sourceRunId"] for event in cached_events])
            self.assertEqual(["agent_1", "agent_2", "agent_3"], [job.job_id for job in loaded.jobs])

    def test_runtime_stops_interrupted_source_resume_plan_at_stale_middle_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            script = """
const first = await agent('first')
const second = await agent('second')
const third = await agent('third')
return [first.summary, second.summary, third.summary]
"""
            source = store.create_run(WorkflowRun(run_id="wf_source", session_id="session_test", script=script, status="running"))
            WorkflowRuntime(
                store=store,
                runner=CountingRunner(
                    results={
                        "agent_1": {"summary": "first cached"},
                        "agent_2": {"summary": "second stale"},
                        "agent_3": {"summary": "third should not reuse"},
                    }
                ),
            ).run(source, args={"same": True})
            source = store.load_run("wf_source")
            source.status = "interrupted"
            source.jobs[1].status = "stale"
            store.save_run(source)

            resumed = store.create_run(WorkflowRun(run_id="wf_resumed", session_id="session_test", script=script, status="running"))
            runner = CountingRunner(
                results={
                    "agent_2": {"summary": "second fresh"},
                    "agent_3": {"summary": "third fresh"},
                }
            )

            outcome = WorkflowRuntime(store=store, runner=runner).run(
                resumed,
                args={"same": True},
                resume_from_run_id="wf_source",
            )

            self.assertEqual(["first cached", "second fresh", "third fresh"], outcome.result)
            self.assertEqual(["agent_2", "agent_3"], runner.started_job_ids)
            loaded = store.load_run("wf_resumed")
            self.assertEqual(["cached", "succeeded", "succeeded"], [job.status for job in loaded.jobs])
            self.assertEqual({"summary": "first cached"}, loaded.jobs[0].metadata["result"])
            self.assertEqual({"summary": "second fresh"}, store.read_agent_result(loaded, loaded.jobs[1]).payload)
            self.assertEqual({"summary": "third fresh"}, store.read_agent_result(loaded, loaded.jobs[2]).payload)
            events = store.replay_events("wf_resumed")
            event_types = [event.event_type for event in events]
            self.assertEqual(1, event_types.count("agent_cached"))
            self.assertEqual(2, event_types.count("agent_started"))
            cached_event = next(event for event in events if event.event_type == "agent_cached")
            self.assertEqual("agent_1", cached_event.payload["sourceJobId"])
            self.assertEqual("wf_source", cached_event.payload["sourceRunId"])
            self.assertNotIn("third should not reuse", json.dumps(outcome.result))

    def test_runtime_stops_interrupted_source_resume_plan_at_running_middle_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            script = """
const first = await agent('first')
const second = await agent('second')
const third = await agent('third')
return [first.summary, second.summary, third.summary]
"""
            source = store.create_run(WorkflowRun(run_id="wf_source", session_id="session_test", script=script, status="running"))
            WorkflowRuntime(
                store=store,
                runner=CountingRunner(
                    results={
                        "agent_1": {"summary": "first cached"},
                        "agent_2": {"summary": "second running"},
                        "agent_3": {"summary": "third should not reuse"},
                    }
                ),
            ).run(source, args={"same": True})
            source = store.load_run("wf_source")
            source.status = "interrupted"
            source.jobs[1].status = "running"
            store.save_run(source)

            resumed = store.create_run(WorkflowRun(run_id="wf_resumed", session_id="session_test", script=script, status="running"))
            runner = CountingRunner(results={"agent_2": {"summary": "second fresh"}, "agent_3": {"summary": "third fresh"}})

            outcome = WorkflowRuntime(store=store, runner=runner).run(resumed, args={"same": True}, resume_from_run_id="wf_source")

            self.assertEqual(["first cached", "second fresh", "third fresh"], outcome.result)
            self.assertEqual(["agent_2", "agent_3"], runner.started_job_ids)
            loaded = store.load_run("wf_resumed")
            self.assertEqual(["cached", "succeeded", "succeeded"], [job.status for job in loaded.jobs])
            event_types = [event.event_type for event in store.replay_events("wf_resumed")]
            self.assertEqual(1, event_types.count("agent_cached"))
            self.assertEqual(2, event_types.count("agent_started"))
            self.assertNotIn("third should not reuse", json.dumps(outcome.result))

    def test_runtime_timeout_kills_never_resolving_async_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            script = "return await new Promise(() => {})"
            run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script=script, status="running"))
            runtime = WorkflowRuntime(store=store, runner=FakeChildAgentRunner(), timeout_seconds=0.2)

            started_at = time.monotonic()
            with self.assertRaisesRegex(RuntimeError, "(?i)(timeout|deadline)"):
                runtime.run(run)

            self.assertLess(time.monotonic() - started_at, 2.0)
            loaded = store.load_run("wf_test")
            self.assertEqual("failed", loaded.status)
            final_result = json.loads((Path(run.artifact_dir) / "final-result.json").read_text(encoding="utf-8"))
            self.assertEqual("failed", final_result["status"])
            self.assertIn("deadline", final_result["error"])
            self.assertEqual("workflow_failed", store.replay_events("wf_test")[-1].event_type)

    def test_runtime_timeout_uses_configured_deadline_for_sync_infinite_loop(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            script = "while (true) {}"
            run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script=script, status="running"))
            runtime = WorkflowRuntime(store=store, runner=FakeChildAgentRunner(), timeout_seconds=0.2)

            started_at = time.monotonic()
            with self.assertRaisesRegex(RuntimeError, "(?i)deadline"):
                runtime.run(run)

            self.assertLess(time.monotonic() - started_at, 2.0)
            loaded = store.load_run("wf_test")
            self.assertEqual("failed", loaded.status)
            self.assertNotIn("1000ms", loaded.error or "")

    def test_runtime_timeout_cancels_running_child_agents(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            runner = NeverFinishesRunner()
            script = "return await agent('never finishes')"
            run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script=script, status="running"))
            runtime = WorkflowRuntime(store=store, runner=runner, timeout_seconds=0.2)

            with self.assertRaisesRegex(RuntimeError, "(?i)deadline"):
                runtime.run(run)

            self.assertEqual({"agent_1"}, runner.cancelled_job_ids)
            loaded = store.load_run("wf_test")
            self.assertEqual("failed", loaded.status)
            self.assertEqual("cancelled", loaded.jobs[0].status)
            final_result = json.loads((Path(run.artifact_dir) / "final-result.json").read_text(encoding="utf-8"))
            self.assertEqual("failed", final_result["status"])

    def test_runtime_worker_error_after_parallel_cancels_pending_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            runner = FailingAndHangingRunner()
            script = """
const results = await parallel([() => agent('fails'), () => agent('still running')])
return results
"""
            run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script=script, status="running"))
            runtime = WorkflowRuntime(store=store, runner=runner, scheduler_config=SchedulerConfig(max_concurrent=2), timeout_seconds=1.0)

            with self.assertRaisesRegex(RuntimeError, "agent one failed"):
                runtime.run(run)

            loaded = store.load_run("wf_test")
            self.assertEqual("failed", loaded.status)
            self.assertEqual("failed", loaded.jobs[0].status)
            self.assertEqual("cancelled", loaded.jobs[1].status)
            self.assertEqual({"agent_2"}, runner.cancelled_job_ids)
            event_types = [event.event_type for event in store.replay_events("wf_test")]
            self.assertIn("agent_failed", event_types)
            self.assertIn("agent_cancelled", event_types)
            self.assertEqual("workflow_failed", event_types[-1])

    def test_runtime_parallel_partial_failure_preserves_success_artifact_and_failed_result(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            runner = SucceedsThenFailsRunner()
            script = """
const results = await parallel([() => agent('succeeds first'), () => agent('fails second')])
return results
"""
            run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script=script, status="running"))
            runtime = WorkflowRuntime(store=store, runner=runner, scheduler_config=SchedulerConfig(max_concurrent=2), timeout_seconds=2.0)

            with self.assertRaisesRegex(RuntimeError, "agent two failed after one success"):
                runtime.run(run)

            loaded = store.load_run("wf_test")
            artifact_dir = Path(loaded.artifact_dir)
            self.assertEqual("failed", loaded.status)
            self.assertIn("agent two failed after one success", loaded.error)
            self.assertEqual(["agent_1", "agent_2"], runner.started_job_ids)
            self.assertEqual([], sorted(runner.cancelled_job_ids))
            self.assertEqual(["succeeded", "failed"], [job.status for job in loaded.jobs])

            success_result_path = artifact_dir / loaded.jobs[0].result_ref
            self.assertTrue(success_result_path.exists())
            success_result = json.loads(success_result_path.read_text(encoding="utf-8"))
            self.assertEqual("succeeded", success_result["status"])
            self.assertEqual("agent one succeeded", success_result["payload"]["summary"])
            self.assertNotIn("transcriptEvents", success_result)
            success_transcript = artifact_dir / loaded.jobs[0].metadata["transcriptRef"]
            self.assertTrue(success_transcript.exists())

            failed_result_path = artifact_dir / loaded.jobs[1].result_ref
            self.assertTrue(failed_result_path.exists())
            failed_result = json.loads(failed_result_path.read_text(encoding="utf-8"))
            self.assertEqual("failed", failed_result["status"])
            self.assertEqual("agent two failed after one success", failed_result["payload"]["error"])
            self.assertNotIn("transcriptEvents", failed_result)
            failed_transcript = artifact_dir / loaded.jobs[1].metadata["transcriptRef"]
            self.assertTrue(failed_transcript.exists())

            final_result = json.loads((artifact_dir / "final-result.json").read_text(encoding="utf-8"))
            self.assertEqual("failed", final_result["status"])
            self.assertIn("agent two failed after one success", final_result["error"])
            event_types = [event.event_type for event in store.replay_events("wf_test")]
            self.assertLess(event_types.index("agent_completed"), event_types.index("agent_failed"))
            self.assertEqual("workflow_failed", event_types[-1])
            failed_event = next(event for event in store.replay_events("wf_test") if event.event_type == "agent_failed")
            self.assertEqual("agent_2", failed_event.job_id)
            self.assertEqual("agent two failed after one success", failed_event.payload["error"])
            self.assertEqual("agents/agent_2/result.json", failed_event.payload["resultRef"])

    def test_handled_child_failure_persists_partial_execution_outcome(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            script = """
const first = await agent('succeeds first')
let handled = false
try {
  await agent('fails second')
} catch (error) {
  handled = true
}
return {handled, first}
"""
            run = store.create_run(
                WorkflowRun(run_id="wf_partial", session_id="session_test", script=script, status="running")
            )

            outcome = WorkflowRuntime(
                store=store,
                runner=SucceedsThenFailsRunner(),
                timeout_seconds=2.0,
            ).run(run)

            self.assertTrue(outcome.result["handled"])
            loaded = store.load_run(run.run_id)
            self.assertEqual("succeeded", loaded.status)
            self.assertEqual("partial", loaded.metadata["executionOutcome"])
            self.assertEqual(1, loaded.metadata["childSummary"]["failed"])
            final_result = json.loads((Path(loaded.artifact_dir) / "final-result.json").read_text(encoding="utf-8"))
            progress = json.loads((Path(loaded.artifact_dir) / "workflow-progress.json").read_text(encoding="utf-8"))
            self.assertEqual("partial", final_result["executionOutcome"])
            self.assertEqual(loaded.metadata["childSummary"], final_result["childSummary"])
            self.assertEqual(loaded.metadata["childSummary"], progress["childSummary"])

    def test_runtime_provider_429_rate_limit_failure_preserves_artifacts_and_events(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            runner = RateLimitAfterFirstSuccessRunner()
            script = """
const results = await parallel([() => agent('succeeds before 429'), () => agent('hits provider 429')])
return results
"""
            run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script=script, status="running"))
            runtime = WorkflowRuntime(store=store, runner=runner, scheduler_config=SchedulerConfig(max_concurrent=2), timeout_seconds=2.0)

            with self.assertRaisesRegex(RuntimeError, "429.*rate limit"):
                runtime.run(run)

            loaded = store.load_run("wf_test")
            artifact_dir = Path(loaded.artifact_dir)
            self.assertEqual("failed", loaded.status)
            self.assertIn("429", loaded.error)
            self.assertIn("rate limit", loaded.error.lower())
            self.assertEqual(["agent_1", "agent_2"], runner.started_job_ids)
            self.assertEqual(["succeeded", "failed"], [job.status for job in loaded.jobs])

            success_result = json.loads((artifact_dir / loaded.jobs[0].result_ref).read_text(encoding="utf-8"))
            self.assertEqual("succeeded", success_result["status"])
            self.assertEqual("agent one succeeded before rate limit", success_result["payload"]["summary"])
            self.assertNotIn("transcriptEvents", success_result)
            self.assertTrue((artifact_dir / loaded.jobs[0].metadata["transcriptRef"]).exists())

            failed_result = json.loads((artifact_dir / loaded.jobs[1].result_ref).read_text(encoding="utf-8"))
            self.assertEqual("failed", failed_result["status"])
            self.assertEqual(429, failed_result["payload"]["statusCode"])
            self.assertEqual("rate_limit", failed_result["payload"]["category"])
            self.assertIn("Too Many Requests", failed_result["payload"]["error"])
            self.assertNotIn("transcriptEvents", failed_result)
            self.assertTrue((artifact_dir / loaded.jobs[1].metadata["transcriptRef"]).exists())

            final_result = json.loads((artifact_dir / "final-result.json").read_text(encoding="utf-8"))
            self.assertEqual("failed", final_result["status"])
            self.assertIn("429", final_result["error"])
            event_types = [event.event_type for event in store.replay_events("wf_test")]
            self.assertLess(event_types.index("agent_completed"), event_types.index("agent_failed"))
            self.assertEqual("workflow_failed", event_types[-1])
            failed_event = next(event for event in store.replay_events("wf_test") if event.event_type == "agent_failed")
            self.assertEqual("agent_2", failed_event.job_id)
            self.assertIn("429", failed_event.payload["error"])
            self.assertEqual("agents/agent_2/result.json", failed_event.payload["resultRef"])

    def test_runtime_observes_external_kill_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            script = """
log('ready for external kill')
return await new Promise(() => {})
"""
            run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script=script, status="running"))
            runtime = WorkflowRuntime(store=store, runner=FakeChildAgentRunner(), timeout_seconds=5.0)
            errors = []

            thread = threading.Thread(target=lambda: self._run_and_capture(runtime, run, errors), daemon=True)
            thread.start()
            deadline = time.monotonic() + 3.0
            while time.monotonic() < deadline:
                if any(event.event_type == "workflow_log" for event in store.replay_events("wf_test")):
                    break
                time.sleep(0.02)
            self.assertTrue(any(event.event_type == "workflow_log" for event in store.replay_events("wf_test")))
            killed = store.load_run("wf_test")
            killed.status = "killed"
            killed.error = "user requested stop"
            store.save_run(killed)
            thread.join(timeout=10.0)

            self.assertFalse(thread.is_alive())
            self.assertTrue(errors)
            self.assertIn("killed", str(errors[0]).lower())
            loaded = store.load_run("wf_test")
            self.assertEqual("killed", loaded.status)
            self.assertEqual("final-result.json", loaded.result_ref)
            final_result = json.loads((Path(run.artifact_dir) / "final-result.json").read_text(encoding="utf-8"))
            self.assertEqual("killed", final_result["status"])
            self.assertEqual("workflow_killed", store.replay_events("wf_test")[-1].event_type)

    def test_runtime_preserves_external_kill_when_cancelling_stale_running_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            run = store.create_run(
                WorkflowRun(
                    run_id="wf_test",
                    session_id="session_test",
                    script="return await new Promise(() => {})",
                    status="running",
                    jobs=[WorkflowJob(job_id="agent_1", prompt="work", status="running")],
                )
            )
            runtime = WorkflowRuntime(store=store, runner=FakeChildAgentRunner(), timeout_seconds=0.1)
            scheduler = AgentScheduler(store=store, run=run, runner=runtime.runner, manage_run_completion=False)
            killed = store.load_run("wf_test")
            killed.status = "killed"
            killed.error = "user requested stop"
            store.save_run(killed)

            runtime._cancel_unfinished_jobs(scheduler, reason="workflow runtime deadline exceeded")

            loaded = store.load_run("wf_test")
            self.assertEqual("killed", loaded.status)
            self.assertEqual("user requested stop", loaded.error)
            self.assertEqual("cancelled", loaded.jobs[0].status)

    def test_runtime_terminate_escalates_to_kill_and_waits(self):
        process = FakeProcessForTerminate()
        runtime = WorkflowRuntime(runner=FakeChildAgentRunner())

        runtime._terminate(process)

        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)
        self.assertEqual(2, process.wait_calls)
        self.assertTrue(process.stdin.closed)
        self.assertTrue(process.stdout.closed)
        self.assertTrue(process.stderr.closed)

    def _run_and_capture(self, runtime, run, errors):
        try:
            runtime.run(run)
        except Exception as exc:  # pragma: no cover - asserted by caller thread
            errors.append(exc)


if __name__ == "__main__":
    unittest.main()
