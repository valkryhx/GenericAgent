import tempfile
import unittest

from workflow_controller import WorkflowController
from workflow_models import WorkflowRun
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


if __name__ == "__main__":
    unittest.main()
