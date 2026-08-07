import unittest
from types import SimpleNamespace

from agent_runtime_models import (
    AgentCapabilities,
    AgentEvent,
    AgentEventBatch,
    AgentRecord,
    AgentResultRecord,
)
from agent_control import ControlRequest, UnifiedAgentControl


def record(execution_id, *, record_kind="process_agent", run_id=None, job_id=None, actions=None):
    actions = actions or {"read", "events", "result"}
    return AgentRecord(
        execution_id=execution_id,
        engine="workflow" if execution_id.startswith("workflow-") else "process",
        record_kind=record_kind,
        status="running",
        run_id=run_id,
        job_id=job_id,
        capabilities=AgentCapabilities(actions=frozenset(actions)),
    )


def event(event_id, engine, execution_id, source_cursor, source_sequence):
    return AgentEvent(
        sequence=source_sequence,
        event_type="agent_completed",
        agent_path="",
        event_id=event_id,
        engine=engine,
        execution_id=execution_id,
        record_kind="workflow_child" if execution_id.startswith("workflow-child") else "process_agent",
        source_sequence=source_sequence,
        source_cursor=source_cursor,
    )


class FakeAdapter:
    def __init__(self, engine, records, events=None, list_error=None):
        self.engine = engine
        self.records = list(records)
        self.events = events or AgentEventBatch()
        self.list_error = list_error
        self.control_calls = 0

    def list_records(self, *, include_terminal=False, path_prefix=None):
        if self.list_error:
            raise self.list_error
        return list(self.records)

    def get_record(self, execution_id):
        return next((item for item in self.records if item.execution_id == execution_id), None)

    def events_since(self, cursors=None, *, execution_id=None):
        if execution_id is None:
            return self.events
        return AgentEventBatch(
            events=tuple(item for item in self.events.events if item.execution_id == execution_id),
            next_cursors=self.events.next_cursors,
            errors=self.events.errors,
        )

    def read_result(self, execution_id, *, include_preview=False):
        return AgentResultRecord(execution_id=execution_id, status="running")

    def control(self, execution_id, request):
        self.control_calls += 1
        return SimpleNamespace(ok=True, code="ok", execution_id=execution_id, scope="execution", status="running")


class UnifiedAgentControlTest(unittest.TestCase):
    def test_list_records_merges_engines_and_sorts_by_execution_id(self):
        process = FakeAdapter("process", [record("process-agent:one")])
        workflow = FakeAdapter("workflow", [record("workflow-child:wf_a:agent_1")])
        control = UnifiedAgentControl([workflow, process])

        self.assertEqual(
            ["process-agent:one", "workflow-child:wf_a:agent_1"],
            [item.execution_id for item in control.list_records(include_terminal=True)],
        )

    def test_get_routes_by_opaque_execution_id_without_assuming_run_equals_job(self):
        expected = record(
            "workflow-child:wf_a:agent_1",
            record_kind="workflow_child",
            run_id="wf_a",
            job_id="agent_1",
        )
        workflow = FakeAdapter("workflow", [expected])
        control = UnifiedAgentControl([workflow])

        self.assertIs(expected, control.get_record(expected.execution_id))
        self.assertIsNone(control.get_record("agent_1"))

    def test_events_since_uses_per_source_cursors_and_deduplicates_event_id(self):
        process_event = event("process:evt_1", "process", "process-agent:one", "process", 4)
        workflow_a_event = event("workflow:wf_a:4", "workflow", "workflow-child:wf_a:agent_1", "workflow:wf_a", 4)
        workflow_b_event = event("workflow:wf_b:4", "workflow", "workflow-child:wf_b:agent_1", "workflow:wf_b", 4)
        duplicate = event("workflow:wf_a:4", "workflow", "workflow-run:wf_b", "workflow:wf_b", 8)
        control = UnifiedAgentControl(
            [
                FakeAdapter("process", [], AgentEventBatch((process_event,), {"process": 4})),
                FakeAdapter(
                    "workflow",
                    [],
                    AgentEventBatch(
                        (workflow_a_event, workflow_b_event, duplicate),
                        {"workflow:wf_a": 4, "workflow:wf_b": 8},
                    ),
                ),
            ]
        )

        batch = control.events_since({"process": 3, "workflow:wf_a": 3, "workflow:wf_b": 3})

        self.assertEqual(
            ["process:evt_1", "workflow:wf_a:4", "workflow:wf_b:4"],
            [item.event_id for item in batch.events],
        )
        self.assertEqual(4, batch.next_cursors["process"])
        self.assertEqual(8, batch.next_cursors["workflow:wf_b"])

    def test_control_routes_only_to_the_owning_adapter(self):
        process = FakeAdapter(
            "process",
            [record("process-agent:one", actions={"read", "events", "result", "interrupt"})],
        )
        workflow = FakeAdapter("workflow", [record("workflow-run:wf_a", record_kind="workflow_run", run_id="wf_a")])
        control = UnifiedAgentControl([process, workflow])

        result = control.control("process-agent:one", ControlRequest(action="interrupt", reason="test"))

        self.assertTrue(result.ok)
        self.assertEqual(1, process.control_calls)
        self.assertEqual(0, workflow.control_calls)

    def test_unsupported_workflow_child_action_is_structured_and_side_effect_free(self):
        child = record(
            "workflow-child:wf_a:agent_1",
            record_kind="workflow_child",
            run_id="wf_a",
            job_id="agent_1",
        )
        workflow = FakeAdapter("workflow", [child])
        control = UnifiedAgentControl([workflow])

        result = control.control(child.execution_id, ControlRequest(action="cancel"))

        self.assertFalse(result.ok)
        self.assertEqual("unsupported_capability", result.code)
        self.assertEqual(0, workflow.control_calls)

    def test_adapter_failure_is_redacted_and_does_not_hide_other_engine_records(self):
        good = FakeAdapter("process", [record("process-agent:one")])
        bad = FakeAdapter("workflow", [], list_error=RuntimeError("Bearer workflow-secret-should-not-leak"))
        control = UnifiedAgentControl([good, bad])

        self.assertEqual(["process-agent:one"], [item.execution_id for item in control.list_records(include_terminal=True)])
        self.assertNotIn("workflow-secret", control.last_errors["workflow"])


if __name__ == "__main__":
    unittest.main()
