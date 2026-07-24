import json
import tempfile
import unittest
from pathlib import Path

from workflow_child_agent import FakeChildAgentRunner
from workflow_controller import WorkflowController
from workflow_models import WorkflowRun
from workflow_planner import WorkflowDraft, WorkflowPlanner
from workflow_runtime import WorkflowRuntime
from workflow_scheduler import SchedulerConfig
from workflow_store import WorkflowStore


class WorkflowControllerTest(unittest.TestCase):
    def test_create_draft_persists_run_without_starting_scheduler(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = WorkflowController(WorkflowStore(root=tmp))

            run = controller.create_draft(session_id="session_test", script="phase('Plan')")

            self.assertEqual("draft", run.status)
            self.assertEqual("session_test", run.session_id)
            self.assertEqual("phase('Plan')", run.script)
            self.assertIsNotNone(run.artifact_dir)
            self.assertEqual(run, controller.store.load_run(run.run_id))
            self.assertEqual([], controller.store.replay_events(run.run_id))

    def test_request_approval_moves_draft_to_awaiting_approval_and_records_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = WorkflowController(WorkflowStore(root=tmp))
            run = controller.create_draft(session_id="session_test", script="")

            updated = controller.request_approval(run.run_id)

            self.assertEqual("awaiting_approval", updated.status)
            events = controller.store.replay_events(run.run_id)
            self.assertEqual(["workflow_approval_requested"], [event.event_type for event in events])
            self.assertEqual([1], [event.sequence for event in events])

    def test_approve_moves_awaiting_approval_to_running_but_does_not_launch_workers(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = WorkflowController(WorkflowStore(root=tmp))
            run = controller.request_approval(
                controller.create_draft(session_id="session_test", script="spawn('agent')").run_id
            )

            updated = controller.approve(run.run_id)

            self.assertEqual("running", updated.status)
            self.assertEqual([], updated.jobs)
            events = controller.store.replay_events(run.run_id)
            self.assertEqual(
                ["workflow_approval_requested", "workflow_started"],
                [event.event_type for event in events],
            )

    def test_deny_moves_awaiting_approval_to_cancelled_with_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = WorkflowController(WorkflowStore(root=tmp))
            run = controller.request_approval(
                controller.create_draft(session_id="session_test", script="").run_id
            )

            updated = controller.deny(run.run_id, reason="not allowed")

            self.assertEqual("cancelled", updated.status)
            self.assertEqual("not allowed", updated.error)
            events = controller.store.replay_events(run.run_id)
            self.assertEqual("workflow_denied", events[-1].event_type)
            self.assertEqual({"reason": "not allowed"}, events[-1].payload)

    def test_cancel_and_stop_record_terminal_states_without_scheduler(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = WorkflowController(WorkflowStore(root=tmp))
            cancel_run = controller.approve(
                controller.request_approval(
                    controller.create_draft(session_id="session_test", script="").run_id
                ).run_id
            )
            stop_run = controller.approve(
                controller.request_approval(
                    controller.create_draft(session_id="session_test", script="").run_id
                ).run_id
            )

            cancelled = controller.cancel(cancel_run.run_id, reason="user cancel")
            killed = controller.stop(stop_run.run_id, reason="user stop")

            self.assertEqual("cancelled", cancelled.status)
            self.assertEqual("killed", killed.status)
            self.assertEqual("workflow_cancelled", controller.store.replay_events(cancel_run.run_id)[-1].event_type)
            self.assertEqual("workflow_killed", controller.store.replay_events(stop_run.run_id)[-1].event_type)

    def test_resume_projects_interrupted_state_through_store(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script="", status="running"))
            controller = WorkflowController(store)

            projected = controller.resume(run.run_id)

            self.assertEqual("interrupted", projected.status)
            self.assertEqual("workflow_interrupted", store.replay_events(run.run_id)[-1].event_type)

    def test_create_planned_run_auto_approves_valid_draft(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = WorkflowController(WorkflowStore(root=tmp))
            planner = WorkflowPlanner()

            run = controller.create_planned_run(
                session_id="session_test",
                task_text="调研 workflow planner control plane",
                planner=planner,
                context={"constraints": ["不要读取 mykey.py", "不要提交"]},
            )

            self.assertEqual("running", run.status)
            self.assertIn("export const meta", run.script)
            self.assertEqual("workflow-draft.json", run.metadata["workflowDraftRef"])
            self.assertEqual("deterministic", run.metadata["plannerMode"])
            self.assertEqual("research", run.metadata["workflowTaskType"])
            persisted = controller.store.load_run(run.run_id)
            self.assertEqual("running", persisted.status)
            self.assertEqual(run.metadata, persisted.metadata)
            draft_path = Path(run.artifact_dir) / "workflow-draft.json"
            draft_data = json.loads(draft_path.read_text(encoding="utf-8"))
            self.assertEqual("调研 workflow planner control plane", draft_data["taskText"])
            self.assertTrue(draft_data["validation"]["ok"])
            events = controller.store.replay_events(run.run_id)
            self.assertEqual(["workflow_planned", "workflow_started"], [event.event_type for event in events])
            self.assertEqual([1, 2], [event.sequence for event in events])
            self.assertEqual("workflow-draft.json", events[0].payload["workflowDraftRef"])
            self.assertEqual("deterministic", events[0].payload["plannerMode"])

    def test_create_planned_run_can_request_approval_when_auto_approve_false(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = WorkflowController(WorkflowStore(root=tmp))

            run = controller.create_planned_run(
                session_id="session_test",
                task_text="调研 workflow planner control plane",
                planner=WorkflowPlanner(),
                context={"constraints": ["不要读取 mykey.py", "不要提交"]},
                auto_approve=False,
            )

            self.assertEqual("awaiting_approval", run.status)
            events = controller.store.replay_events(run.run_id)
            self.assertEqual(
                ["workflow_planned", "workflow_approval_requested"],
                [event.event_type for event in events],
            )

    def test_create_planned_run_records_rejected_draft_without_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = WorkflowController(WorkflowStore(root=tmp))
            draft = WorkflowDraft(
                task_text="坏计划",
                context={"plannerMode": "prompt_guided_rejected"},
                classification={"taskType": "coding"},
                plan={"taskType": "coding", "phases": []},
                validation={"ok": False, "mode": "rejected", "issues": [{"code": "missing_phase"}]},
                script="",
            )

            class RejectedPlanner:
                def plan(self, task_text, context=None):
                    return draft

            run = controller.create_planned_run(
                session_id="session_test",
                task_text="坏计划",
                planner=RejectedPlanner(),
            )

            self.assertEqual("failed", run.status)
            self.assertEqual("workflow_plan_rejected", run.error)
            self.assertEqual("prompt_guided_rejected", run.metadata["plannerMode"])
            self.assertEqual("workflow-draft.json", run.metadata["workflowDraftRef"])
            self.assertEqual("", run.script)
            events = controller.store.replay_events(run.run_id)
            self.assertEqual(["workflow_planned", "workflow_plan_rejected"], [event.event_type for event in events])
            self.assertEqual([{"code": "missing_phase"}], events[-1].payload["issues"])

    def test_create_planned_run_records_fallback_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            controller = WorkflowController(WorkflowStore(root=tmp))
            draft = WorkflowPlanner().plan("调研 fallback", context={"constraints": ["不要读取 mykey.py", "不要提交"]})
            draft.context["plannerMode"] = "fallback_deterministic"
            draft.validation["mode"] = "fallback_deterministic"

            class FallbackPlanner:
                def plan(self, task_text, context=None):
                    return draft

            run = controller.create_planned_run(
                session_id="session_test",
                task_text="调研 fallback",
                planner=FallbackPlanner(),
            )

            self.assertEqual("running", run.status)
            self.assertEqual("fallback_deterministic", run.metadata["plannerMode"])
            self.assertEqual("fallback_deterministic", controller.store.replay_events(run.run_id)[0].payload["plannerMode"])

    def test_create_planned_run_script_executes_with_fake_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            controller = WorkflowController(store)
            run = controller.create_planned_run(
                session_id="session_test",
                task_text="调研 Claude Code dynamic workflow",
                planner=WorkflowPlanner(),
                context={"constraints": ["不要读取 mykey.py", "不要提交"]},
            )

            outcome = WorkflowRuntime(
                store=store,
                runner=FakeChildAgentRunner(),
                scheduler_config=SchedulerConfig(max_concurrent=2, max_total=3),
                timeout_seconds=5.0,
            ).run(run)

            self.assertEqual("succeeded", outcome.run.status)
            self.assertEqual(["workflow_planned", "workflow_started"], [event.event_type for event in store.replay_events(run.run_id)[:2]])
            self.assertEqual("workflow-draft.json", store.load_run(run.run_id).metadata["workflowDraftRef"])


if __name__ == "__main__":
    unittest.main()
