import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path

from agent_runtime_models import make_process_execution_id
from subagent_artifacts import SubagentArtifactStore
from subagent_manager import AgentState


class FakeEventBus:
    def __init__(self, events):
        self.events = list(events)

    def read_events_since(self, since_event_seq=0, *, targets=None):
        target_set = {str(item).rstrip("/").split("/")[-1] for item in (targets or [])}
        return [
            event
            for event in self.events
            if int(event.get("event_seq") or 0) > int(since_event_seq or 0)
            and (not target_set or event.get("task_name") in target_set)
        ]


class FakeManager:
    def __init__(self, states, *, events=None, temp_dir=None):
        self.states = list(states)
        self.event_bus = FakeEventBus(events or [])
        self.temp_dir = Path(temp_dir or ".")
        self.interrupt_calls = []
        self.close_calls = []
        self.message_calls = []
        self.followup_calls = []
        self.resume_calls = []
        self.attach_calls = []
        self.detach_calls = []

    def list_agents(self, *args, **kwargs):
        raise AssertionError("process adapter must use list_agent_snapshots")

    def read_agent(self, *args, **kwargs):
        raise AssertionError("process adapter must use probe_agent")

    def list_agent_snapshots(self, path_prefix=None, include_closed=False):
        states = self.states
        if path_prefix:
            states = [state for state in states if state.agent_path.startswith(path_prefix)]
        if not include_closed:
            states = [state for state in states if state.process_status not in {"shutdown", "killed"}]
        return list(states)

    def probe_agent(self, target):
        for state in self.states:
            if target in {state.task_name, state.agent_path}:
                return state
        raise FileNotFoundError(target)

    def interrupt_agent(self, target, reason=""):
        self.interrupt_calls.append((target, reason))

    def close_agent(self, target, reason="", cascade=False, **kwargs):
        self.close_calls.append((target, cascade))
        return {"closedDescendantExecutionIds": ["process-agent:child"]}

    def send_message(self, target, message, **kwargs):
        self.message_calls.append((target, message))
        return {"target": target, "message": message}

    def followup_task(self, target, message, **kwargs):
        self.followup_calls.append((target, message))
        return {"target": target, "message": message}

    def resume_agent(self, target, message, **kwargs):
        self.resume_calls.append((target, message))
        return {"target": target, "message": message}

    def attach_agent(self, target, **kwargs):
        self.attach_calls.append((target, kwargs))
        return {"target": target}

    def detach_agent(self, target, **kwargs):
        self.detach_calls.append((target, kwargs))
        return {"target": target}


class ProcessSubagentAdapterTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def make_state(
        self,
        *,
        run_id="run_demo",
        task_name="demo",
        task_dir=None,
        turn_status="running",
        process_status="alive",
        permission_profile="inherit-current-permissions",
        worktree_path=None,
        final_output_path=None,
    ):
        task_path = Path(task_dir or self.tmp.name) / "temp" / task_name
        task_path.mkdir(parents=True, exist_ok=True)
        output_path = task_path / "output.txt"
        output_path.write_text("bounded process output\n", encoding="utf-8")
        artifact_dir = Path(self.tmp.name) / "temp" / "subagents" / "runs" / run_id
        artifact_dir.mkdir(parents=True, exist_ok=True)
        final_path = Path(final_output_path) if final_output_path else output_path
        final_path.parent.mkdir(parents=True, exist_ok=True)
        final_path.write_text("final process output\n", encoding="utf-8")
        artifact = SubagentArtifactStore(artifact_dir).record_final_output(final_path, round_no=0)
        return AgentState(
            task_name=task_name,
            agent_path=f"/root/{task_name}",
            pid=123,
            task_dir=str(task_path),
            turn_status=turn_status,
            process_status=process_status,
            round=0,
            output_path=str(output_path),
            final_output_path=str(final_path),
            parent_session_id="session_demo",
            run_id=run_id,
            artifact_dir=str(artifact_dir),
            permission_profile=permission_profile,
            parent_permission_mode="read_only",
            permission_options={"allow": ["file_read"]},
            agent_type="worker",
            worktree_path=worktree_path,
        ), artifact

    def test_completed_process_turn_maps_to_succeeded_but_keeps_waiting_process_status(self):
        state, _artifact = self.make_state(turn_status="completed", process_status="waiting_reply")
        from agent_control_process import ProcessSubagentAdapter

        record = ProcessSubagentAdapter(FakeManager([state], temp_dir=self.tmp.name)).list_records(include_terminal=True)[0]
        self.assertEqual("succeeded", record.status)
        self.assertEqual("completed", record.turn_status)
        self.assertEqual("waiting_reply", record.process_status)

    def test_process_record_keeps_identity_workspace_and_permission_metadata(self):
        task_dir = Path(self.tmp.name) / "ga" / "temp" / "demo"
        state, _artifact = self.make_state(
            run_id="run_0007",
            task_name="demo",
            task_dir=task_dir.parent.parent,
            permission_profile="read_only",
            worktree_path="C:/ga/worktrees/run_0007",
        )
        from agent_control_process import ProcessSubagentAdapter

        record = ProcessSubagentAdapter(FakeManager([state], temp_dir=self.tmp.name)).list_records(include_terminal=True)[0]
        self.assertEqual("run_0007", record.run_id)
        self.assertEqual("/root/demo", record.agent_path)
        self.assertEqual(str(task_dir), record.workspace)
        self.assertEqual("read_only", record.permission_profile)
        self.assertEqual("C:/ga/worktrees/run_0007", record.metadata["worktreePath"])

    def test_process_capabilities_include_only_real_process_actions(self):
        state, _artifact = self.make_state()
        from agent_control_process import ProcessSubagentAdapter

        record = ProcessSubagentAdapter(FakeManager([state], temp_dir=self.tmp.name)).list_records(include_terminal=True)[0]
        for action in ("read", "events", "result", "artifacts", "interrupt", "close", "message", "followup", "resume", "attach", "detach"):
            self.assertTrue(record.capabilities.supports_action(action), action)
        self.assertFalse(record.capabilities.supports_action("cancel"))

    def test_process_event_uses_event_bus_sequence_and_artifact_reference(self):
        state, artifact = self.make_state(run_id="run_0008", final_output_path=Path(self.tmp.name) / "output.txt")
        raw = {
            "event_seq": 12,
            "event_id": "evt_000012",
            "type": "turn_completed",
            "agent_path": "/root/demo",
            "run_id": "run_0008",
            "created_at": "2026-08-06T00:00:00Z",
            "status": {"turn_status": "completed", "process_status": "waiting_reply"},
            "payload": {"final_output_ref": artifact["artifact_id"]},
            "task_name": "demo",
        }
        manager = FakeManager([state], events=[raw], temp_dir=self.tmp.name)
        from agent_control_process import ProcessSubagentAdapter

        events = ProcessSubagentAdapter(manager).events_since({"process": 11})
        self.assertEqual([12], [event.source_sequence for event in events.events])
        self.assertEqual("process:evt_000012", events.events[0].event_id)
        self.assertEqual(12, events.next_cursors["process"])

    def test_process_result_is_reference_only_by_default(self):
        state, _artifact = self.make_state(run_id="run_0009")
        manager = FakeManager([state], temp_dir=self.tmp.name)
        from agent_control_process import ProcessSubagentAdapter

        result = ProcessSubagentAdapter(manager).read_result(make_process_execution_id(state.run_id, state.agent_path))
        self.assertEqual({}, result.payload)
        self.assertIsNotNone(result.final_text_ref)
        self.assertIsNotNone(result.transcript_ref)

    def test_close_result_object_maps_descendant_paths_to_scoped_execution_ids(self):
        parent, _parent_artifact = self.make_state(run_id="run_parent", task_name="parent")
        child, _child_artifact = self.make_state(run_id="run_child", task_name="child")
        manager = FakeManager([parent, child], temp_dir=self.tmp.name)
        result = SimpleNamespace(
            closed_descendants=[
                {"agent_path": child.agent_path, "task_name": child.task_name},
            ]
        )
        from agent_control_process import ProcessSubagentAdapter

        adapter = ProcessSubagentAdapter(manager)
        self.assertEqual(
            [make_process_execution_id(child.run_id, child.agent_path)],
            adapter._close_descendant_execution_ids(result),
        )


if __name__ == "__main__":
    unittest.main()
