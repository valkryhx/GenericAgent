import tempfile
import unittest
from types import SimpleNamespace
from pathlib import Path

from genericagent.core.models import make_process_execution_id
from genericagent.agents.artifacts import SubagentArtifactStore
from genericagent.agents.manager import AgentState


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
        self.session_data_dir = self.temp_dir

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
        from genericagent.core.control.process import ProcessSubagentAdapter

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
        from genericagent.core.control.process import ProcessSubagentAdapter

        record = ProcessSubagentAdapter(FakeManager([state], temp_dir=self.tmp.name)).list_records(include_terminal=True)[0]
        self.assertEqual("run_0007", record.run_id)
        self.assertEqual("/root/demo", record.agent_path)
        self.assertEqual(str(task_dir), record.workspace)
        self.assertEqual("read_only", record.permission_profile)
        self.assertEqual("C:/ga/worktrees/run_0007", record.metadata["worktreePath"])

    def test_process_capabilities_include_only_real_process_actions(self):
        state, _artifact = self.make_state()
        from genericagent.core.control.process import ProcessSubagentAdapter

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
        from genericagent.core.control.process import ProcessSubagentAdapter

        events = ProcessSubagentAdapter(manager).events_since({"process": 11})
        self.assertEqual([12], [event.source_sequence for event in events.events])
        self.assertEqual("process:evt_000012", events.events[0].event_id)
        self.assertEqual(12, events.next_cursors["process"])

    def test_process_result_is_reference_only_by_default(self):
        state, _artifact = self.make_state(run_id="run_0009")
        manager = FakeManager([state], temp_dir=self.tmp.name)
        from genericagent.core.control.process import ProcessSubagentAdapter

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
        from genericagent.core.control.process import ProcessSubagentAdapter

        adapter = ProcessSubagentAdapter(manager)
        self.assertEqual(
            [make_process_execution_id(child.run_id, child.agent_path)],
            adapter._close_descendant_execution_ids(result),
        )


class ProcessStatusProjectionWithoutWorkVerdictTest(unittest.TestCase):
    """A child that has no work verdict must not look like it produced one.

    The reported symptom: in `ga`, a subagent occasionally flashes a red
    `✗ Subagent <name> failed` the moment it starts. The chain:

      * `_refresh_state` rewrites a `pending` turn to `running` when the child is not
        alive, and `_project_status` sees `process_status == 'exited'` and returns
        `closed`;
      * `completion_status` was never assigned a work verdict, so it stays `pending`;
      * the UI's verdict chain is a positive check on success words, so `pending`
        fell through to `failed`.

    `closed` has to stay terminal -- `list_records(include_terminal=False)` filters on
    `_TERMINAL_STATUSES`, and a dead child must not linger in the live list. So the
    contract this class pins is narrower: the record must *carry* the absence of a
    verdict, so consumers can tell "closed after failing" from "closed before it
    ever ran". `metadata.completionStatus`/`turnStatus` are that channel, and the UI
    reads them (`subagentPendingVerdict.test.ts`).
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def make_state(self, *, turn_status, process_status, completion_status=None):
        return AgentState(
            task_name="sub1",
            agent_path="/root/sub1",
            pid=None,
            task_dir=str(Path(self.tmp.name) / "temp" / "sub1"),
            turn_status=turn_status,
            process_status=process_status,
            round=0,
            output_path=None,
            final_output_path=None,
            completion_status=completion_status,
            run_id="run_flicker",
        )

    def project(self, state):
        from genericagent.core.control.process import ProcessSubagentAdapter

        return ProcessSubagentAdapter._project_status(state)

    def record_for(self, state):
        from genericagent.core.control.process import ProcessSubagentAdapter

        return ProcessSubagentAdapter(FakeManager([state], temp_dir=self.tmp.name)).list_records(
            include_terminal=True
        )[0]

    def test_child_that_exited_before_a_verdict_carries_no_verdict(self):
        """The reproduced shape must be readable as "no verdict", not as a failure.

        `_refresh_state` had rewritten the turn to `running`, which downstream reads as
        work-in-progress. What the record must preserve is that no verdict exists.
        """
        state = self.make_state(turn_status="running", process_status="exited")
        record = self.record_for(state)
        completion = (record.metadata or {}).get("completionStatus")
        self.assertIn(
            completion,
            (None, "pending"),
            f"a child with no work verdict claimed completionStatus={completion!r}",
        )

    def test_a_dead_child_still_counts_as_terminal_so_it_does_not_linger(self):
        """The invariant the fix must NOT break: `closed` is filtered out of the live list."""
        from genericagent.core.control.process import ProcessSubagentAdapter, _TERMINAL_STATUSES

        state = self.make_state(turn_status="running", process_status="exited")
        status = self.project(state)
        self.assertIn(status, _TERMINAL_STATUSES, f"{status} would linger in the live agent list")

        live = ProcessSubagentAdapter(FakeManager([state], temp_dir=self.tmp.name)).list_records()
        self.assertEqual([], live, "a child with no verdict must not be reported as live work")

    def test_real_verdicts_still_project_to_their_statuses(self):
        """The fix must not make the projection blind to real outcomes."""
        cases = [
            # (turn_status, process_status, completion_status, expected)
            ("completed", "exited", "completed", "succeeded"),
            ("errored", "exited", "failed", "failed"),
            ("errored", "exited", None, "failed"),
            ("interrupted", "exited", None, "interrupted"),
            ("running", "killed", None, "killed"),
        ]
        for turn_status, process_status, completion, expected in cases:
            with self.subTest(turn_status=turn_status, process_status=process_status, completion=completion):
                status = self.project(self.make_state(
                    turn_status=turn_status,
                    process_status=process_status,
                    completion_status=completion,
                ))
                self.assertEqual(expected, status)

    def test_completion_verdict_beats_a_live_process_status(self):
        status = self.project(self.make_state(
            turn_status="completed",
            process_status="waiting_reply",
            completion_status="completed",
        ))
        self.assertEqual("succeeded", status)

    def test_an_exited_child_with_no_verdict_does_not_report_a_completed_turn(self):
        """`turnStatus` is the field the UI trusts second. It must not claim a turn
        completed when the child produced no output."""
        state = self.make_state(turn_status="running", process_status="exited")
        record = self.record_for(state)
        self.assertNotEqual(
            "completed",
            (record.metadata or {}).get("turnStatus"),
            "a child with no output reported a completed turn",
        )


if __name__ == "__main__":
    unittest.main()
