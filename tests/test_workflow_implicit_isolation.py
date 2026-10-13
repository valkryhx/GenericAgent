import tempfile
import unittest
from pathlib import Path

from workflow_models import WorkflowRun as _WorkflowRun
from workflow_runtime import WorkflowRuntime
from workflow_scheduler import (
    AgentScheduler,
    FakeChildAgentRunner,
    SchedulerConfig,
    _normalize_parallel_batch,
)
from workflow_store import WorkflowStore
from workflow_tool_profiles import declares_writes


_CC_META_PREFIX = "export const meta = { name: 'iso-test', description: 'implicit isolation test' }" + chr(10)


class WorkflowRun(_WorkflowRun):
    """Test adapter that prepends the CC meta contract to legacy bodies."""

    def __init__(self, *args, **kwargs):
        script = kwargs.get("script")
        if isinstance(script, str) and script and not script.lstrip().startswith("export const meta"):
            kwargs["script"] = _CC_META_PREFIX + script
        super().__init__(*args, **kwargs)


class DeclaresWritesTest(unittest.TestCase):
    def test_write_scope_or_deliverables_make_a_writer(self):
        self.assertTrue(declares_writes({"writeScope": ["a.py"]}))
        self.assertTrue(declares_writes({"deliverables": ["out/report.md"]}))
        self.assertTrue(declares_writes({"writeScope": "a.py"}))

    def test_declared_write_or_execute_capability_makes_a_writer(self):
        self.assertTrue(declares_writes({"capabilities": ["file_write"]}))
        self.assertTrue(declares_writes({"capabilities": ["execute"]}))

    def test_required_mutating_tool_makes_a_writer(self):
        self.assertTrue(declares_writes({"requiredTools": ["file_write"]}))
        self.assertTrue(declares_writes({"requiredTools": ["code_run"]}))

    def test_read_only_job_is_not_a_writer(self):
        self.assertFalse(declares_writes({}))
        self.assertFalse(declares_writes({"capabilities": ["web_search", "file_read"]}))
        self.assertFalse(declares_writes({"requiredTools": ["file_read"]}))
        self.assertFalse(declares_writes({"writeScope": []}))


class NormalizeParallelBatchTest(unittest.TestCase):
    def test_only_a_real_batch_of_two_or_more_survives(self):
        self.assertIsNone(_normalize_parallel_batch(None))
        self.assertIsNone(_normalize_parallel_batch(1))
        self.assertIsNone(_normalize_parallel_batch("x"))
        self.assertEqual(2, _normalize_parallel_batch(2))
        self.assertEqual(3, _normalize_parallel_batch("3"))


class ImplicitIsolationSchedulerTest(unittest.TestCase):
    def make_scheduler(self, *, max_concurrent=4):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = WorkflowStore(root=tmp.name)
        run = store.create_run(
            WorkflowRun(run_id="wf_test", session_id="session_test", script="spawn agents", status="running")
        )
        scheduler = AgentScheduler(
            store=store,
            run=run,
            runner=FakeChildAgentRunner(),
            config=SchedulerConfig(max_concurrent=max_concurrent, max_total=1000),
        )
        return scheduler, store, run

    def isolation(self, job):
        return (job.metadata.get("options") or {}).get("isolation")

    def test_parallel_writer_is_implicitly_isolated(self):
        scheduler, store, run = self.make_scheduler()
        job = scheduler.register_agent(
            prompt="write a.py", label="w1", options={"writeScope": ["a.py"]}, parallel_batch=2
        )
        scheduler.tick()
        self.assertEqual("worktree", self.isolation(job))
        self.assertEqual("implicit-parallel-writer", job.metadata["isolationReason"])
        self.assertIn("agent_isolated", [event.event_type for event in store.replay_events(run.run_id)])

    def test_parallel_non_writer_is_not_isolated(self):
        scheduler, _store, _run = self.make_scheduler()
        job = scheduler.register_agent(prompt="research", label="r1", options={}, parallel_batch=3)
        scheduler.tick()
        self.assertIsNone(self.isolation(job))
        self.assertNotIn("isolationReason", job.metadata)

    def test_serial_writer_is_not_isolated(self):
        scheduler, _store, _run = self.make_scheduler()
        job = scheduler.register_agent(prompt="write a.py", label="w1", options={"writeScope": ["a.py"]})
        scheduler.tick()
        self.assertIsNone(self.isolation(job))

    def test_explicit_none_opts_out_of_implicit_isolation(self):
        scheduler, _store, _run = self.make_scheduler()
        job = scheduler.register_agent(
            prompt="write a.py",
            label="w1",
            options={"writeScope": ["a.py"], "isolation": "none"},
            parallel_batch=2,
        )
        scheduler.tick()
        self.assertEqual("none", self.isolation(job))
        self.assertNotIn("isolationReason", job.metadata)

    def test_explicit_worktree_is_kept_without_implicit_reason(self):
        scheduler, _store, _run = self.make_scheduler()
        job = scheduler.register_agent(
            prompt="write a.py",
            label="w1",
            options={"writeScope": ["a.py"], "isolation": "worktree"},
            parallel_batch=2,
        )
        scheduler.tick()
        self.assertEqual("worktree", self.isolation(job))
        self.assertNotIn("isolationReason", job.metadata)

    def test_serial_run_never_implicitly_isolates(self):
        scheduler, _store, _run = self.make_scheduler(max_concurrent=1)
        job = scheduler.register_agent(
            prompt="write a.py", label="w1", options={"writeScope": ["a.py"]}, parallel_batch=2
        )
        scheduler.tick()
        self.assertIsNone(self.isolation(job))


class ImplicitIsolationRuntimeTest(unittest.TestCase):
    def test_runtime_implicitly_isolates_a_parallel_writer(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            script = """
const results = await parallel([
  () => agent('write a.py', {label: 'w1', writeScope: ['a.py']}),
  () => agent('write b.py', {label: 'w2', writeScope: ['b.py']}),
])
return {summary: 'ok'}
"""
            run = store.create_run(
                WorkflowRun(run_id="wf_test", session_id="session_test", script=script, status="running")
            )
            runner = _RecordingRunner()
            WorkflowRuntime(store=store, runner=runner, timeout_seconds=5.0).run(
                run, args={"workspacePath": str(workspace)}
            )
            self.assertEqual(2, len(runner.started_jobs))
            for job in runner.started_jobs:
                self.assertEqual("worktree", (job.metadata.get("options") or {}).get("isolation"))
                self.assertEqual("implicit-parallel-writer", job.metadata.get("isolationReason"))

    def test_runtime_accepts_isolation_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            script = """
const result = await agent('write a.py', {label: 'w1', isolation: 'none', writeScope: ['a.py']})
return {summary: result.summary}
"""
            run = store.create_run(
                WorkflowRun(run_id="wf_test", session_id="session_test", script=script, status="running")
            )
            runner = _RecordingRunner()
            WorkflowRuntime(store=store, runner=runner, timeout_seconds=5.0).run(
                run, args={"workspacePath": str(workspace)}
            )
            self.assertEqual("none", (runner.started_jobs[0].metadata.get("options") or {}).get("isolation"))


class _RecordingRunner(FakeChildAgentRunner):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.started_jobs = []

    def start(self, job):
        self.started_jobs.append(job)
        super().start(job)


if __name__ == "__main__":
    unittest.main()
