import json
import tempfile
import threading
import time
import unittest
from pathlib import Path

from workflow_child_agent import AgentResult, FakeChildAgentRunner
from workflow_models import WorkflowRun
from workflow_runtime import WorkflowRuntime
from workflow_scheduler import SchedulerConfig
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


class CountingRunner(FakeChildAgentRunner):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.started_job_ids = []

    def start(self, job):
        self.started_job_ids.append(job.job_id)
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
            events = store.replay_events("wf_test")
            self.assertEqual(
                ["workflow_phase", "workflow_log", "agent_registered", "agent_started", "agent_completed"],
                [event.event_type for event in events],
            )
            final_result = json.loads((Path(run.artifact_dir) / "final-result.json").read_text(encoding="utf-8"))
            self.assertEqual("succeeded", final_result["status"])
            self.assertEqual("ok", final_result["result"]["summary"])
            self.assertEqual("agent_1", final_result["jobs"][0]["jobId"])

    def test_runtime_rejects_forbidden_script_tokens_before_worker_starts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script="return process.env", status="running"))
            runtime = WorkflowRuntime(store=store)

            with self.assertRaises(ValueError):
                runtime.run(run)

            self.assertEqual([], store.replay_events("wf_test"))

    def test_runtime_marks_run_failed_when_worker_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script="throw new Error('boom')", status="running"))
            runtime = WorkflowRuntime(store=store)

            with self.assertRaises(RuntimeError):
                runtime.run(run)

            loaded = store.load_run("wf_test")
            self.assertEqual("failed", loaded.status)
            self.assertIn("boom", loaded.error)
            events = store.replay_events("wf_test")
            self.assertEqual("workflow_failed", events[-1].event_type)
            final_result = json.loads((Path(run.artifact_dir) / "final-result.json").read_text(encoding="utf-8"))
            self.assertEqual("failed", final_result["status"])

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

    def test_runtime_timeout_kills_never_resolving_async_script(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            script = "return await new Promise(() => {})"
            run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script=script, status="running"))
            runtime = WorkflowRuntime(store=store, timeout_seconds=0.2)

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
            runtime = WorkflowRuntime(store=store, timeout_seconds=0.2)

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

    def test_runtime_observes_external_kill_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            script = "return await new Promise(() => {})"
            run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script=script, status="running"))
            runtime = WorkflowRuntime(store=store, timeout_seconds=5.0)
            errors = []

            thread = threading.Thread(target=lambda: self._run_and_capture(runtime, run, errors), daemon=True)
            thread.start()
            time.sleep(0.2)
            killed = store.load_run("wf_test")
            killed.status = "killed"
            killed.error = "user requested stop"
            store.save_run(killed)
            thread.join(timeout=2.0)

            self.assertFalse(thread.is_alive())
            self.assertTrue(errors)
            self.assertIn("killed", str(errors[0]).lower())
            loaded = store.load_run("wf_test")
            self.assertEqual("killed", loaded.status)
            final_result = json.loads((Path(run.artifact_dir) / "final-result.json").read_text(encoding="utf-8"))
            self.assertEqual("killed", final_result["status"])
            self.assertEqual("workflow_killed", store.replay_events("wf_test")[-1].event_type)

    def test_runtime_terminate_escalates_to_kill_and_waits(self):
        process = FakeProcessForTerminate()
        runtime = WorkflowRuntime()

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
