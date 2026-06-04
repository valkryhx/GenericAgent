import json
import tempfile
import unittest
from pathlib import Path

from workflow_models import AgentResult, WorkflowEvent, WorkflowJob, WorkflowRun
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

    def test_append_permission_event_maps_raw_event_to_workflow_journal_event(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            run = WorkflowRun(
                run_id="wf_test",
                session_id="session_test",
                script="",
                jobs=[WorkflowJob(job_id="agent_1", prompt="work")],
            )
            run = store.create_run(run)
            store.append_event(run, WorkflowEvent(run_id="wf_test", event_type="agent_started", sequence=1, job_id="agent_1"))
            raw_event = {
                "type": "tool_denied",
                "runId": "wf_test",
                "jobId": "agent_1",
                "toolName": "file_write",
                "profile": "read_only",
                "decision": "deny",
                "reason": "read_only_static_write_or_execute",
                "permission": {
                    "action": "deny",
                    "reason": "read_only_static_write_or_execute",
                    "profile": "read_only",
                    "toolName": "file_write",
                    "details": {},
                },
            }

            stored = store.append_permission_event(run, raw_event)

            self.assertEqual("tool_denied", stored.event_type)
            self.assertEqual("wf_test", stored.run_id)
            self.assertEqual("session_test", stored.session_id)
            self.assertEqual("agent_1", stored.job_id)
            self.assertEqual(2, stored.sequence)
            self.assertEqual("file_write", stored.payload["toolName"])
            self.assertEqual("read_only", stored.payload["profile"])
            self.assertEqual("deny", stored.payload["decision"])
            self.assertEqual("read_only_static_write_or_execute", stored.payload["reason"])
            self.assertEqual("deny", stored.payload["permission"]["action"])
            events = store.replay_events("wf_test")
            self.assertEqual(["agent_started", "tool_denied"], [event.event_type for event in events])
            self.assertEqual(stored, events[-1])

    def test_append_permission_event_rejects_raw_event_from_different_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            run = store.create_run(WorkflowRun(run_id="wf_test", session_id="session_test", script=""))
            raw_event = {
                "type": "tool_allowed",
                "runId": "wf_other",
                "jobId": "agent_1",
                "toolName": "file_read",
                "profile": "read_only",
                "decision": "allow",
                "reason": "read_only_static_safe",
                "permission": {"action": "allow", "reason": "read_only_static_safe"},
            }

            with self.assertRaises(ValueError):
                store.append_permission_event(run, raw_event)

            self.assertEqual([], store.replay_events("wf_test"))

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

    def test_write_agent_result_persists_result_artifact_and_updates_job_ref(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            run = WorkflowRun(
                run_id="wf_test",
                session_id="session_test",
                script="",
                jobs=[WorkflowJob(job_id="agent_1", prompt="work")],
            )
            run = store.create_run(run)
            job = run.jobs[0]
            result = AgentResult(
                job_id="agent_1",
                payload={"summary": "ok"},
                transcript_ref="agents/agent_1/transcript.jsonl",
                token_usage={"input_tokens": 1},
                tool_summary={"Read": 2},
                transcript_events=[{"type": "assistant", "text": "verbose"}],
            )

            result_ref = store.write_agent_result(run, job, result)

            self.assertEqual("agents/agent_1/result.json", result_ref)
            self.assertEqual(result_ref, job.result_ref)
            result_path = Path(run.artifact_dir) / result_ref
            data = json.loads(result_path.read_text(encoding="utf-8"))
            self.assertEqual("agent_1", data["jobId"])
            self.assertEqual({"summary": "ok"}, data["payload"])
            self.assertEqual("agents/agent_1/transcript.jsonl", data["transcriptRef"])
            self.assertEqual({"input_tokens": 1}, data["tokenUsage"])
            self.assertEqual({"Read": 2}, data["toolSummary"])
            self.assertNotIn("transcriptEvents", data)

    def test_read_agent_result_loads_persisted_result_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            run = WorkflowRun(
                run_id="wf_test",
                session_id="session_test",
                script="",
                jobs=[WorkflowJob(job_id="agent_1", prompt="work")],
            )
            run = store.create_run(run)
            job = run.jobs[0]
            store.write_agent_result(
                run,
                job,
                AgentResult(
                    job_id="agent_1",
                    payload={"summary": "ok"},
                    transcript_ref="agents/agent_1/transcript.jsonl",
                    token_usage={"input_tokens": 1},
                    tool_summary={"Read": 2},
                ),
            )

            loaded = store.read_agent_result(run, job)

            self.assertEqual("agent_1", loaded.job_id)
            self.assertEqual("succeeded", loaded.status)
            self.assertEqual({"summary": "ok"}, loaded.payload)
            self.assertEqual("agents/agent_1/transcript.jsonl", loaded.transcript_ref)
            self.assertEqual({"input_tokens": 1}, loaded.token_usage)
            self.assertEqual({"Read": 2}, loaded.tool_summary)

    def test_write_agent_transcript_persists_jsonl_under_agent_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            run = WorkflowRun(
                run_id="wf_test",
                session_id="session_test",
                script="",
                jobs=[WorkflowJob(job_id="agent_1", prompt="work")],
            )
            run = store.create_run(run)
            job = run.jobs[0]
            events = [
                {"type": "metadata", "runId": "wf_test", "jobId": "agent_1"},
                {"type": "assistant", "text": "ok"},
            ]

            transcript_ref = store.write_agent_transcript(run, job, events)

            self.assertEqual("agents/agent_1/transcript.jsonl", transcript_ref)
            transcript_path = Path(run.artifact_dir) / transcript_ref
            lines = transcript_path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(2, len(lines))
            self.assertEqual(events, [json.loads(line) for line in lines])

    def test_mark_running_jobs_stale_on_resume_projection(self):
        for event_type in ("agent_started", "job_running"):
            with self.subTest(event_type=event_type):
                with tempfile.TemporaryDirectory() as tmp:
                    store = WorkflowStore(root=tmp)
                    run = WorkflowRun(run_id="wf_test", session_id="session_test", script="", status="running")
                    run = store.create_run(run)
                    store.append_event(run, WorkflowEvent(run_id="wf_test", event_type=event_type, sequence=1, job_id="agent_1"))

                    projected = store.project_resume_state("wf_test")

                    self.assertEqual("interrupted", projected.status)
                    self.assertEqual("stale", projected.jobs[0].status)
                    events = store.replay_events("wf_test")
                    self.assertEqual("workflow_interrupted", events[-1].event_type)


if __name__ == "__main__":
    unittest.main()
