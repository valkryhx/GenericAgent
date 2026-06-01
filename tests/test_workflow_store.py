import json
import tempfile
import unittest
from pathlib import Path

from workflow_models import WorkflowEvent, WorkflowRun
from workflow_store import WorkflowStore


class WorkflowStoreTest(unittest.TestCase):
    def test_create_run_writes_artifact_files_under_session_workflows_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            run = WorkflowRun(run_id="wf_test", session_id="session_test", script="log('hi')")

            created = store.create_run(run)

            artifact_dir = Path(tmp) / "session_test" / "workflows" / "wf_test"
            self.assertEqual(str(artifact_dir), created.artifact_dir)
            self.assertEqual("log('hi')", (artifact_dir / "script.js").read_text(encoding="utf-8"))
            self.assertTrue((artifact_dir / "journal.jsonl").exists())
            run_json = json.loads((artifact_dir / "run.json").read_text(encoding="utf-8"))
            self.assertEqual("inherit-current-permissions", run_json["permissionProfile"])
            self.assertEqual("inherit-current-v1", run_json["permissionPolicyVersion"])

    def test_append_journal_is_append_only_and_replayable(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script=""))

            store.append_event(
                run,
                WorkflowEvent(run_id="wf_test", event_type="workflow_started", sequence=1),
            )
            store.append_event(
                "wf_test",
                WorkflowEvent(run_id="wf_test", event_type="workflow_completed", sequence=2),
            )

            events = store.replay_events("wf_test")
            self.assertEqual(["workflow_started", "workflow_completed"], [event.event_type for event in events])
            self.assertEqual([1, 2], [event.sequence for event in events])
            lines = (Path(run.artifact_dir) / "journal.jsonl").read_text(encoding="utf-8").splitlines()
            self.assertEqual(2, len(lines))

    def test_load_run_round_trips_current_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script="phase('A')"))
            run.status = "awaiting_approval"

            store.save_run(run)
            loaded = store.load_run("wf_test")

            self.assertEqual(run, loaded)
            self.assertEqual("awaiting_approval", loaded.status)
            self.assertEqual("phase('A')", loaded.script)

    def test_mark_running_jobs_stale_on_resume_projection(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            run = WorkflowRun(run_id="wf_test", session_id="session_test", script="", status="running")
            run = store.create_run(run)
            store.append_event(run, WorkflowEvent(run_id="wf_test", event_type="job_running", sequence=1, job_id="agent_1"))

            projected = store.project_resume_state("wf_test")

            self.assertEqual("interrupted", projected.status)
            events = store.replay_events("wf_test")
            self.assertEqual("workflow_interrupted", events[-1].event_type)


if __name__ == "__main__":
    unittest.main()
