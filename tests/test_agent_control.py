import unittest
import tempfile
from types import SimpleNamespace
from pathlib import Path

from agent_runtime_models import (
    AgentCapabilities,
    AgentEvent,
    AgentEventBatch,
    AgentRecord,
    AgentResultRecord,
    make_process_execution_id,
    make_workflow_child_execution_id,
    make_workflow_run_execution_id,
)
from agent_control import ControlRequest, ControlResult, UnifiedAgentControl
from subagent_manager import AgentState
from workflow_controller import WorkflowController
from workflow_models import WorkflowJob, WorkflowRun
from workflow_store import WorkflowStore


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


def make_process_record(name):
    task_name = str(name).split("/")[-1].split(":")[-1]
    agent_path = f"/root/{task_name}"
    run_id = f"run-{task_name}"
    return AgentRecord(
        execution_id=make_process_execution_id(run_id, agent_path),
        engine="process",
        record_kind="process_agent",
        status="running",
        run_id=run_id,
        agent_path=agent_path,
        task_name=task_name,
        turn_status="running",
        process_status="alive",
        workspace="C:/ga/temp",
        capabilities=AgentCapabilities(
            actions=frozenset(
                {
                    "read",
                    "events",
                    "result",
                    "artifacts",
                    "interrupt",
                    "close",
                    "message",
                    "followup",
                    "resume",
                    "attach",
                    "detach",
                }
            )
        ),
    )


class RecordingProcessManager:
    def __init__(self, records):
        self.records = list(records)
        self.states = [
            AgentState(
                task_name=record.task_name,
                agent_path=record.agent_path,
                pid=100,
                task_dir=record.workspace or "C:/ga/temp",
                turn_status="running",
                process_status="alive",
                round=0,
                output_path=None,
                final_output_path=None,
                run_id=record.run_id,
                permission_profile="read_only",
            )
            for record in self.records
        ]
        self.interrupt_calls = []
        self.close_calls = []
        self.event_bus = SimpleNamespace(read_events_since=lambda _cursor: [])

    def list_agent_snapshots(self, path_prefix=None, include_closed=False):
        return list(self.states)

    def probe_agent(self, target):
        for state in self.states:
            if target in {state.agent_path, state.task_name}:
                return state
        raise FileNotFoundError(target)

    def interrupt_agent(self, target, reason=""):
        self.interrupt_calls.append((target, reason))

    def close_agent(self, target, reason="", cascade=False, **kwargs):
        self.close_calls.append((target, cascade))
        return {"closedDescendantExecutionIds": ["process-agent:child"]}


class RecordingWorkflowController(WorkflowController):
    def __init__(self, store):
        super().__init__(store=store)
        self.stop_calls = []
        self.cancel_calls = []

    def stop(self, run_id, *, reason=""):
        self.stop_calls.append(run_id)
        return super().stop(run_id, reason=reason)

    def cancel(self, run_id, *, reason=""):
        self.cancel_calls.append(run_id)
        return super().cancel(run_id, reason=reason)


class UnifiedAgentControlTest(unittest.TestCase):
    def setUp(self):
        self._temporary_workspaces = []
        self.addCleanup(self._cleanup_workspaces)

    def _cleanup_workspaces(self):
        for workspace in self._temporary_workspaces:
            workspace.cleanup()

    def recording_running_workflow(self, run_id, child_count):
        workspace = tempfile.TemporaryDirectory()
        self._temporary_workspaces.append(workspace)
        store = WorkflowStore(Path(workspace.name) / "sessions")
        controller = RecordingWorkflowController(store)
        run = WorkflowRun(
            run_id=run_id,
            session_id="session_control",
            script="return {}",
            status="running",
            jobs=[WorkflowJob(job_id=f"agent_{index}", status="running") for index in range(1, child_count + 1)],
        )
        return store, controller, store.create_run(run)

    def test_process_interrupt_is_forwarded_with_reason(self):
        process_record = make_process_record("one")
        manager = RecordingProcessManager([process_record])
        from agent_control_process import ProcessSubagentAdapter

        control = UnifiedAgentControl([ProcessSubagentAdapter(manager)])
        result = control.control(process_record.execution_id, ControlRequest(action="interrupt", reason="user stop"))

        self.assertTrue(result.ok)
        self.assertEqual(("/root/one", "user stop"), manager.interrupt_calls[0])
        self.assertEqual("execution", result.scope)

    def test_process_close_cascade_returns_closed_descendants(self):
        process_record = make_process_record("parent")
        manager = RecordingProcessManager([process_record])
        from agent_control_process import ProcessSubagentAdapter

        control = UnifiedAgentControl([ProcessSubagentAdapter(manager)])
        result = control.control(process_record.execution_id, ControlRequest(action="close", payload={"cascade": True}))

        self.assertTrue(result.ok)
        self.assertTrue(manager.close_calls[0][1])
        self.assertEqual("agent_tree", result.scope)
        self.assertEqual(["process-agent:child"], result.data["closedDescendantExecutionIds"])

    def test_workflow_run_stop_is_scoped_to_the_run_record(self):
        store, controller, run = self.recording_running_workflow("wf_a", child_count=2)
        from agent_control_workflow import WorkflowChildAdapter

        control = UnifiedAgentControl([WorkflowChildAdapter(store, controller)])
        result = control.control(make_workflow_run_execution_id(run.run_id), ControlRequest(action="stop", reason="test stop"))

        self.assertTrue(result.ok)
        self.assertEqual([run.run_id], controller.stop_calls)
        self.assertEqual("workflow_run", result.scope)
        self.assertEqual("killed", store.load_run(run.run_id).status)

    def test_workflow_child_cancel_returns_unsupported_without_cancelling_siblings(self):
        store, controller, run = self.recording_running_workflow("wf_b", child_count=2)
        from agent_control_workflow import WorkflowChildAdapter

        control = UnifiedAgentControl([WorkflowChildAdapter(store, controller)])
        child_id = make_workflow_child_execution_id(run.run_id, "agent_1")
        result = control.control(child_id, ControlRequest(action="cancel"))
        current = store.load_run(run.run_id)

        self.assertFalse(result.ok)
        self.assertEqual("unsupported_capability", result.code)
        self.assertEqual("running", current.status)
        self.assertEqual(["running", "running"], [job.status for job in current.jobs])
        self.assertEqual([], controller.stop_calls)

    def test_workflow_child_resume_returns_unsupported_not_cached_resume(self):
        store, controller, run = self.recording_running_workflow("wf_c", child_count=1)
        from agent_control_workflow import WorkflowChildAdapter

        control = UnifiedAgentControl([WorkflowChildAdapter(store, controller)])
        result = control.control(make_workflow_child_execution_id(run.run_id, "agent_1"), ControlRequest(action="resume"))

        self.assertFalse(result.ok)
        self.assertEqual("unsupported_capability", result.code)
        self.assertEqual("running", store.load_run(run.run_id).status)

    def test_control_result_contains_capability_and_scope_for_ui(self):
        process_record = make_process_record("one")
        result = ControlResult(
            ok=False,
            code="unsupported_capability",
            execution_id=process_record.execution_id,
            scope="execution",
            status=process_record.status,
            data={"requestedAction": "cancel", "capabilities": sorted(process_record.capabilities.actions)},
        )

        self.assertEqual("execution", result.scope)
        self.assertIn("interrupt", result.data["capabilities"])

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
