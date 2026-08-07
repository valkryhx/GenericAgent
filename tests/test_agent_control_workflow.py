import tempfile
import unittest
from pathlib import Path

from agent_runtime_models import (
    make_workflow_child_execution_id,
    make_workflow_run_execution_id,
)
from workflow_controller import WorkflowController
from workflow_models import AgentResult, WorkflowEvent, WorkflowJob, WorkflowRun
from workflow_store import WorkflowStore


class WorkflowAdapterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.store = WorkflowStore(Path(self.tmp.name) / "sessions")
        self.controller = WorkflowController(store=self.store)

    def make_run(self, run_id, jobs, status="running"):
        run = WorkflowRun(run_id=run_id, session_id="session_p2_1", script="return {}", status=status, jobs=jobs)
        return self.store.create_run(run)

    def test_workflow_adapter_exposes_run_container_and_child_records(self):
        self.make_run("wf_a", [WorkflowJob(job_id="agent_1", status="running")])
        from agent_control_workflow import WorkflowChildAdapter

        records = WorkflowChildAdapter(self.store, self.controller).list_records(include_terminal=True)
        self.assertEqual({"workflow_run", "workflow_child"}, {record.record_kind for record in records})
        child = next(record for record in records if record.record_kind == "workflow_child")
        self.assertEqual(make_workflow_run_execution_id("wf_a"), child.parent_execution_id)

    def test_same_job_id_in_two_runs_has_two_execution_ids(self):
        for run_id in ("wf_a", "wf_b"):
            self.make_run(run_id, [WorkflowJob(job_id="agent_1", status="queued")])
        from agent_control_workflow import WorkflowChildAdapter

        children = [
            record
            for record in WorkflowChildAdapter(self.store, self.controller).list_records(include_terminal=True)
            if record.record_kind == "workflow_child"
        ]
        expected = {
            make_workflow_child_execution_id("wf_a", "agent_1"),
            make_workflow_child_execution_id("wf_b", "agent_1"),
        }
        self.assertEqual(expected, {record.execution_id for record in children})

    def test_cached_child_keeps_cache_source_without_fake_attempt_id(self):
        job = WorkflowJob(
            job_id="agent_1",
            status="cached",
            metadata={
                "cachedFromRunId": "wf_source",
                "cachedFromJobId": "agent_1",
            },
        )
        self.make_run("wf_resumed", [job], status="succeeded")
        from agent_control_workflow import WorkflowChildAdapter

        children = [
            record
            for record in WorkflowChildAdapter(self.store, self.controller).list_records(include_terminal=True)
            if record.record_kind == "workflow_child"
        ]
        self.assertEqual(1, len(children))
        child = children[0]
        self.assertEqual(make_workflow_child_execution_id("wf_resumed", "agent_1"), child.execution_id)
        self.assertEqual("cached", child.status)
        self.assertTrue(child.cached)
        self.assertEqual("wf_source", child.metadata["cachedFromRunId"])
        self.assertEqual("agent_1", child.metadata["cachedFromJobId"])
        self.assertIsNone(child.logical_key)
        self.assertIsNone(child.attempt_id)
        self.assertIsNone(child.attempt_index)
        self.assertIsNone(child.attempt_count)

    def test_succeeded_run_with_failed_child_projects_to_partial(self):
        self.make_run(
            "wf_partial",
            [WorkflowJob(job_id="agent_1", status="succeeded"), WorkflowJob(job_id="agent_2", status="failed")],
            status="succeeded",
        )
        from agent_control_workflow import WorkflowChildAdapter

        records = WorkflowChildAdapter(self.store, self.controller).list_records(include_terminal=True)
        run_record = next(record for record in records if record.record_kind == "workflow_run")
        self.assertEqual("succeeded", run_record.source_status)
        self.assertEqual("partial", run_record.status)
        self.assertEqual(1, run_record.metadata["childSummary"]["failed"])
        self.assertEqual("partial", run_record.metadata["executionOutcome"])

    def test_workflow_job_preserves_cached_stale_failed_and_cancelled(self):
        jobs = [
            WorkflowJob(job_id=f"agent_{index}", status=status)
            for index, status in enumerate(("cached", "stale", "failed", "cancelled"), start=1)
        ]
        self.make_run("wf_status", jobs)
        from agent_control_workflow import WorkflowChildAdapter

        children = [
            record
            for record in WorkflowChildAdapter(self.store, self.controller).list_records(include_terminal=True)
            if record.record_kind == "workflow_child"
        ]
        expected = {"cached", "stale", "failed", "cancelled"}
        self.assertEqual(expected, {record.status for record in children})
        self.assertEqual(expected, {record.source_status for record in children})

    def test_workflow_child_record_carries_workspace_permission_transcript_and_result_ref(self):
        workspace = str(Path(self.tmp.name) / "workspace")
        Path(workspace).mkdir()
        job = WorkflowJob(
            job_id="agent_1",
            status="succeeded",
            result_ref="agents/agent_1/result.json",
            metadata={
                "workspacePath": workspace,
                "permissionProfile": "read_only",
                "permissionPolicyVersion": "read-only-v1",
                "transcriptRef": "agents/agent_1/transcript.jsonl",
            },
        )
        run = self.make_run("wf_meta", [job])
        self.store.write_agent_result(
            run,
            job,
            AgentResult(job_id="agent_1", payload={"summary": "done"}, transcript_ref="agents/agent_1/transcript.jsonl"),
        )
        self.store.write_agent_transcript(run, job, [{"type": "capability_snapshot", "capabilities": {"fileReadAvailable": True}}])
        from agent_control_workflow import WorkflowChildAdapter

        child = next(
            record
            for record in WorkflowChildAdapter(self.store, self.controller).list_records(include_terminal=True)
            if record.record_kind == "workflow_child"
        )
        self.assertEqual(workspace, child.workspace)
        self.assertEqual("read_only", child.permission_profile)
        self.assertEqual("read-only-v1", child.permission_policy_version)
        self.assertEqual("agents/agent_1/transcript.jsonl", child.transcript_ref)
        self.assertIn("agents/agent_1/result.json", {ref.ref for ref in child.artifact_refs})
        self.assertTrue(child.capability_snapshot["fileReadAvailable"])

    def test_workflow_journal_events_use_per_run_source_cursor_and_stable_id(self):
        run = self.make_run("wf_events", [WorkflowJob(job_id="agent_1", status="succeeded")])
        self.store.append_event(
            run,
            WorkflowEvent(
                run_id=run.run_id,
                event_type="agent_completed",
                sequence=9,
                job_id="agent_1",
                payload={"resultRef": "agents/agent_1/result.json"},
            ),
        )
        from agent_control_workflow import WorkflowChildAdapter

        events = WorkflowChildAdapter(self.store, self.controller).events_since({"workflow:wf_events": 8})
        self.assertEqual(["workflow:wf_events:9"], [event.event_id for event in events.events])
        self.assertEqual("workflow:wf_events", events.events[0].source_cursor)
        self.assertEqual(9, events.next_cursors["workflow:wf_events"])

    def test_workflow_child_does_not_advertise_process_only_actions(self):
        self.make_run("wf_caps", [WorkflowJob(job_id="agent_1", status="running")])
        from agent_control_workflow import WorkflowChildAdapter

        child = next(
            record
            for record in WorkflowChildAdapter(self.store, self.controller).list_records(include_terminal=True)
            if record.record_kind == "workflow_child"
        )
        for action in ("read", "events", "result", "artifacts"):
            self.assertTrue(child.capabilities.supports_action(action), action)
        for action in ("cancel", "close", "message", "followup", "resume", "attach", "detach"):
            self.assertFalse(child.capabilities.supports_action(action), action)


if __name__ == "__main__":
    unittest.main()
