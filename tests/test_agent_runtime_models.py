import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from agent_runtime_models import AgentEvent, AgentStatus, ArtifactRef  # noqa: E402


class AgentRuntimeModelsTest(unittest.TestCase):
    def test_process_subagent_event_converts_to_common_event(self):
        raw = {
            "event_seq": 7,
            "type": "turn_completed",
            "agent_path": "/root/demo",
            "run_id": "run_demo",
            "status": {"turn_status": "completed", "process_status": "waiting_reply"},
            "payload": {"final_output_ref": "final_output_round_0"},
        }

        event = AgentEvent.from_subagent_event(raw)

        self.assertEqual(event.sequence, 7)
        self.assertEqual(event.event_type, "turn_completed")
        self.assertEqual(event.agent_path, "/root/demo")
        self.assertEqual(event.run_id, "run_demo")
        self.assertEqual(event.status.turn_status, "completed")
        self.assertEqual(event.artifact_ref.artifact_id, "final_output_round_0")

    def test_execution_ids_are_engine_scoped_and_do_not_equal_job_id(self):
        from agent_runtime_models import (
            make_workflow_child_execution_id,
            make_workflow_run_execution_id,
            make_workflow_source_cursor,
        )

        first = make_workflow_child_execution_id("wf_a", "agent_1")
        second = make_workflow_child_execution_id("wf_b", "agent_1")
        self.assertNotEqual(first, second)
        self.assertNotEqual(first, "agent_1")
        self.assertNotEqual(first, make_workflow_run_execution_id("wf_a"))
        self.assertEqual("workflow:wf_a", make_workflow_source_cursor("wf_a"))

    def test_logical_identity_is_stable_across_physical_attempts(self):
        from agent_runtime_models import AgentRecord, make_workflow_child_execution_id

        execution_id = make_workflow_child_execution_id("wf_retry", "agent_1")
        first = AgentRecord(
            execution_id=execution_id,
            engine="workflow",
            record_kind="workflow_child",
            status="running",
            run_id="wf_retry",
            job_id="agent_1",
            logical_key="v2:logical-hash",
            attempt_id="physical-old",
            attempt_index=1,
            attempt_count=2,
        )
        current = AgentRecord(
            execution_id=execution_id,
            engine="workflow",
            record_kind="workflow_child",
            status="succeeded",
            run_id="wf_retry",
            job_id="agent_1",
            logical_key="v2:logical-hash",
            attempt_id="physical-new",
            attempt_index=2,
            attempt_count=2,
        )
        self.assertEqual(first.execution_id, current.execution_id)
        self.assertEqual(first.logical_key, current.logical_key)
        self.assertNotEqual(first.attempt_id, current.attempt_id)
        self.assertNotEqual(current.attempt_id, current.execution_id)

    def test_cached_status_is_not_serialized_as_fresh_success(self):
        from agent_runtime_models import AgentRecord

        record = AgentRecord(
            execution_id="workflow-child:wf_cache:agent_1",
            engine="workflow",
            record_kind="workflow_child",
            status="cached",
            source_status="cached",
            cached=True,
        )
        restored = AgentRecord.from_dict(record.to_dict())
        self.assertEqual("cached", restored.status)
        self.assertEqual("cached", restored.source_status)
        self.assertTrue(restored.cached)

    def test_partial_status_round_trips_without_rewriting_source_status(self):
        from agent_runtime_models import AgentRecord

        record = AgentRecord(
            execution_id="workflow-run:wf_partial",
            engine="workflow",
            record_kind="workflow_run",
            status="partial",
            source_status="succeeded",
            metadata={"childSummary": {"total": 2, "succeeded": 1, "failed": 1}},
        )
        restored = AgentRecord.from_dict(record.to_dict())
        self.assertEqual("partial", restored.status)
        self.assertEqual("succeeded", restored.source_status)
        self.assertEqual(1, restored.metadata["childSummary"]["failed"])

    def test_capabilities_report_unsupported_actions_explicitly(self):
        from agent_runtime_models import AgentCapabilities

        capabilities = AgentCapabilities(actions=frozenset({"read", "result"}))
        self.assertTrue(capabilities.supports("result"))
        self.assertFalse(capabilities.supports("resume"))
        self.assertFalse(capabilities.supports_action("resume"))

    def test_workflow_event_preserves_source_cursor_and_identity(self):
        from agent_runtime_models import make_workflow_child_execution_id, make_workflow_run_execution_id

        execution_id = make_workflow_child_execution_id("wf_a", "agent_1")
        parent_id = make_workflow_run_execution_id("wf_a")
        event = AgentEvent.from_workflow_event(
            {
                "type": "agent_completed",
                "runId": "wf_a",
                "jobId": "agent_1",
                "sequence": 9,
                "payload": {"resultRef": "agents/agent_1/result.json"},
            },
            execution_id=execution_id,
            parent_execution_id=parent_id,
        )
        self.assertEqual("workflow", event.engine)
        self.assertEqual(9, event.source_sequence)
        self.assertEqual(execution_id, event.execution_id)
        self.assertEqual("workflow:wf_a", event.source_cursor)
        self.assertEqual("workflow:wf_a:9", event.event_id)
        self.assertEqual(parent_id, event.parent_execution_id)

    def test_process_conversion_adds_scoped_identity_without_changing_legacy_fields(self):
        from agent_runtime_models import make_process_execution_id

        execution_id = make_process_execution_id("run_1", "/root/demo")
        event = AgentEvent.from_subagent_event(
            {
                "event_seq": 7,
                "event_id": "evt_000007",
                "type": "turn_completed",
                "agent_path": "/root/demo",
                "run_id": "run_1",
                "created_at": "2026-08-06T00:00:00Z",
                "status": {"turn_status": "completed", "process_status": "waiting_reply"},
                "payload": {"final_output_ref": "final_output_round_0"},
            },
            execution_id=execution_id,
        )
        self.assertEqual(7, event.sequence)
        self.assertEqual("process", event.engine)
        self.assertEqual(execution_id, event.execution_id)
        self.assertEqual("process", event.source_cursor)
        self.assertEqual("process:evt_000007", event.event_id)

    def test_event_batch_round_trips_per_source_cursors(self):
        from agent_runtime_models import AgentEventBatch

        batch = AgentEventBatch(next_cursors={"process": 4, "workflow:wf_a": 9})
        restored = AgentEventBatch.from_dict(batch.to_dict())
        self.assertEqual({"process": 4, "workflow:wf_a": 9}, restored.next_cursors)


if __name__ == "__main__":
    unittest.main()
