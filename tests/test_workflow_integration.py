import json
import tempfile
import unittest
from pathlib import Path

import session_transcript
from workflow_controller import WorkflowController
from workflow_scheduler import AgentScheduler, FakeChildAgentRunner, SchedulerConfig
from workflow_store import WorkflowStore


class WorkflowIntegrationTest(unittest.TestCase):
    def test_fake_runner_workflow_slice_persists_artifacts_and_keeps_parent_transcript_isolated(self):
        with tempfile.TemporaryDirectory() as transcript_root, tempfile.TemporaryDirectory() as workflow_root:
            session_id = "session_parent"
            transcript_path = session_transcript.create_session(
                root=transcript_root,
                cwd="D:/git_codes/GenericAgent",
                session_id=session_id,
                frontend="test",
            )
            parent_before = []
            parent_after = [
                {"role": "user", "content": [{"type": "text", "text": "plan workflow"}]},
                {"role": "assistant", "content": [{"type": "text", "text": "draft ready"}]},
            ]
            session_transcript.record_turn(
                transcript_path,
                session_id=session_id,
                turn_id=1,
                source="user",
                user_text="plan workflow",
                assistant_text="draft ready",
                backend_history_before=parent_before,
                backend_history_after=parent_after,
            )

            store = WorkflowStore(root=workflow_root)
            controller = WorkflowController(store)
            draft = controller.create_draft(session_id=session_id, script="spawn fake agents")
            awaiting = controller.request_approval(draft.run_id)
            run = controller.approve(awaiting.run_id)
            session_transcript.record_workflow_event(
                transcript_path,
                session_id=session_id,
                run_id=run.run_id,
                event_type="workflow_started",
                artifact_dir=run.artifact_dir,
            )

            scheduler = AgentScheduler(
                store=store,
                run=run,
                runner=FakeChildAgentRunner(
                    results={
                        "agent_1": {"summary": "scouted repository"},
                        "agent_2": {"summary": "checked workflow artifacts"},
                    }
                ),
                config=SchedulerConfig(max_concurrent=2),
            )
            first = scheduler.register_agent(
                prompt="inspect repo without real llm",
                label="Scout",
                options={"effort": "low"},
            )
            second = scheduler.register_agent(
                prompt="verify artifacts without js worker",
                label="Verifier",
                options={"effort": "low"},
            )

            completed = scheduler.run_all()
            final_run = store.load_run(run.run_id)
            session_transcript.record_workflow_event(
                transcript_path,
                session_id=session_id,
                run_id=run.run_id,
                event_type="workflow_completed",
                artifact_dir=run.artifact_dir,
                result_ref=final_run.result_ref,
            )

            self.assertEqual([first.job_id, second.job_id], [job.job_id for job in completed])
            self.assertEqual("succeeded", final_run.status)
            self.assertEqual("final-result.json", final_run.result_ref)
            final_result = json.loads((Path(final_run.artifact_dir) / final_run.result_ref).read_text(encoding="utf-8"))
            self.assertEqual("succeeded", final_result["status"])
            self.assertEqual(["agent_1", "agent_2"], [job["jobId"] for job in final_result["jobs"]])
            self.assertEqual("succeeded", final_run.jobs[0].status)
            self.assertEqual("succeeded", final_run.jobs[1].status)
            self.assertEqual("agents/agent_1/result.json", final_run.jobs[0].result_ref)
            self.assertEqual("agents/agent_2/result.json", final_run.jobs[1].result_ref)
            first_result_path = Path(final_run.artifact_dir) / "agents" / first.job_id / "result.json"
            second_result_path = Path(final_run.artifact_dir) / "agents" / second.job_id / "result.json"
            self.assertEqual(
                {"summary": "scouted repository"},
                json.loads(first_result_path.read_text(encoding="utf-8"))["payload"],
            )
            self.assertEqual(
                {"summary": "checked workflow artifacts"},
                json.loads(second_result_path.read_text(encoding="utf-8"))["payload"],
            )

            journal_types = [event.event_type for event in store.replay_events(run.run_id)]
            for event_type in [
                "workflow_approval_requested",
                "workflow_started",
                "agent_registered",
                "agent_started",
                "agent_completed",
            ]:
                self.assertIn(event_type, journal_types)
            self.assertEqual(2, journal_types.count("agent_registered"))
            self.assertEqual(2, journal_types.count("agent_started"))
            self.assertEqual(2, journal_types.count("agent_completed"))

            cache_key = final_run.jobs[0].metadata["cacheKey"]
            self.assertEqual("inherit-current-permissions", cache_key["permissionProfile"])
            self.assertEqual("inherit-current-v1", cache_key["permissionPolicyVersion"])

            loaded_session = session_transcript.load_session(transcript_path)
            self.assertEqual(1, loaded_session.rounds)
            self.assertEqual(parent_after, loaded_session.backend_history)
            self.assertEqual(
                [
                    {"role": "user", "content": "plan workflow"},
                    {"role": "assistant", "content": "draft ready"},
                ],
                loaded_session.ui_messages,
            )
            self.assertEqual(1, len(loaded_session.turns))
            self.assertEqual("plan workflow", loaded_session.turns[0].user_text)
            self.assertNotIn("scouted repository", json.dumps(loaded_session.ui_messages, ensure_ascii=False))
            self.assertNotIn("checked workflow artifacts", json.dumps(loaded_session.backend_history, ensure_ascii=False))

            transcript_events = [
                json.loads(line)
                for line in Path(transcript_path).read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            self.assertEqual(["workflow_started", "workflow_completed"], [event["type"] for event in transcript_events[-2:]])
            self.assertEqual(run.run_id, transcript_events[-1]["run_id"])


if __name__ == "__main__":
    unittest.main()
