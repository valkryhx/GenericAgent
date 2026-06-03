import json
import tempfile
import unittest
from pathlib import Path

from workflow_child_agent import FakeChildAgentRunner
from workflow_models import WorkflowRun
from workflow_runtime import WorkflowRuntime
from workflow_store import WorkflowStore


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


if __name__ == "__main__":
    unittest.main()
