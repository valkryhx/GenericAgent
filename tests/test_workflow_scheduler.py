import json
import tempfile
import unittest
from pathlib import Path

from workflow_models import AgentResult, WorkflowRun
from workflow_scheduler import AgentScheduler, FakeChildAgentRunner, SchedulerConfig
from workflow_store import WorkflowStore


class FailedResultRunner:
    def start(self, job):
        pass

    def poll(self, job):
        return AgentResult(
            job_id=job.job_id,
            status="failed",
            payload={"error": "api down"},
            transcript_ref=f"agents/{job.job_id}/transcript.jsonl",
            transcript_events=[{"type": "error", "error": "api down"}],
        )

    def cancel(self, job):
        pass


class ProtocolMetadataRunner:
    def __init__(self):
        self.started = []
        self.cancelled = []

    def start(self, job):
        self.started.append(job.job_id)

    def poll(self, job):
        return AgentResult(
            job_id=job.job_id,
            payload={"summary": "real-ish child result", "text": "verbose child transcript text"},
            transcript_ref=f"agents/{job.job_id}/transcript.jsonl",
            token_usage={"input_tokens": 5, "output_tokens": 7},
            tool_summary={},
            transcript_events=[
                {"type": "metadata", "runId": "wf_test", "jobId": job.job_id},
                {"type": "assistant", "text": "verbose child transcript text"},
            ],
        )

    def cancel(self, job):
        self.cancelled.append(job.job_id)


class PermissionEventsRunner:
    def __init__(self, *, status="succeeded"):
        self.status = status

    def start(self, job):
        pass

    def poll(self, job):
        return AgentResult(
            job_id=job.job_id,
            status=self.status,
            payload={"summary": "permission checked"} if self.status == "succeeded" else {"error": "permission failure"},
            transcript_events=[
                {"type": "metadata", "runId": "wf_test", "jobId": job.job_id},
                {
                    "type": "permission_profile_selected",
                    "runId": "wf_test",
                    "jobId": job.job_id,
                    "toolName": "file_read",
                    "profile": "read_only",
                    "decision": "allow",
                    "reason": "read_only_static_safe",
                    "permission": {"action": "allow", "reason": "read_only_static_safe", "profile": "read_only"},
                },
                {
                    "type": "tool_allowed",
                    "runId": "wf_test",
                    "jobId": job.job_id,
                    "toolName": "file_read",
                    "profile": "read_only",
                    "decision": "allow",
                    "reason": "read_only_static_safe",
                    "permission": {"action": "allow", "reason": "read_only_static_safe", "profile": "read_only"},
                },
                {
                    "type": "tool_denied",
                    "runId": "wf_test",
                    "jobId": job.job_id,
                    "toolName": "file_write",
                    "profile": "read_only",
                    "decision": "deny",
                    "reason": "read_only_static_write_or_execute",
                    "permission": {"action": "deny", "reason": "read_only_static_write_or_execute", "profile": "read_only"},
                },
            ],
        )

    def cancel(self, job):
        pass


class WorkflowSchedulerTest(unittest.TestCase):
    def make_scheduler(self, *, max_concurrent=4, max_total=1000, runner=None, run_kwargs=None):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = WorkflowStore(root=tmp.name)
        run_data = {"run_id": "wf_test", "session_id": "session_test", "script": "spawn agents", "status": "running"}
        run_data.update(run_kwargs or {})
        run = store.create_run(WorkflowRun(**run_data))
        scheduler = AgentScheduler(
            store=store,
            run=run,
            runner=runner or FakeChildAgentRunner(),
            config=SchedulerConfig(max_concurrent=max_concurrent, max_total=max_total),
        )
        return scheduler, store, run

    def event_types(self, store):
        return [event.event_type for event in store.replay_events("wf_test")]

    def test_scheduler_caps_max_concurrent_at_16(self):
        with self.assertRaises(ValueError):
            SchedulerConfig(max_concurrent=17)

    def test_registers_job_with_cache_key_permission_fields_and_journal_event(self):
        scheduler, store, run = self.make_scheduler()

        job = scheduler.register_agent(prompt="inspect repo", label="Scout", options={"effort": "low"})

        self.assertEqual("queued", job.status)
        self.assertEqual(0, job.metadata["callIndex"])
        self.assertEqual("wf_test", job.metadata["runId"])
        self.assertEqual("inherit-current-permissions", job.metadata["permissionProfile"])
        self.assertEqual("inherit-current-v1", job.metadata["permissionPolicyVersion"])
        cache_key = job.metadata["cacheKey"]
        self.assertEqual("inherit-current-permissions", cache_key["permissionProfile"])
        self.assertEqual("inherit-current-v1", cache_key["permissionPolicyVersion"])
        self.assertIn("scriptHash", cache_key)
        self.assertIn("argsHash", cache_key)
        self.assertIn("callIndex", cache_key)
        self.assertIn("promptHash", cache_key)
        self.assertIn("optionsHash", cache_key)
        self.assertEqual(["agent_registered"], self.event_types(store))
        event = store.replay_events(run.run_id)[0]
        self.assertEqual(job.job_id, event.job_id)
        self.assertEqual(cache_key, event.payload["cacheKey"])
        progress = json.loads((Path(run.artifact_dir) / "workflow-progress.json").read_text(encoding="utf-8"))
        entry = progress["workflowProgress"][0]
        self.assertEqual("queued", entry["state"])
        self.assertEqual("Scout", entry["label"])
        self.assertEqual("agent_1", entry["agentId"])
        self.assertEqual("agent_1", entry["jobId"])
        self.assertIn("inspect repo", entry["promptPreview"])

    def test_register_agent_rejects_non_dict_options_with_clear_error(self):
        scheduler, _store, _run = self.make_scheduler()

        with self.assertRaisesRegex(TypeError, "agent options must be a plain object"):
            scheduler.register_agent(prompt="inspect repo", options=[["label", "Scout"]])

        self.assertEqual([], scheduler.jobs)

    def test_register_cached_agent_rejects_non_dict_options_with_clear_error(self):
        scheduler, _store, _run = self.make_scheduler()
        result = AgentResult(job_id="agent_1", status="succeeded", payload={"summary": "ok"})

        with self.assertRaisesRegex(TypeError, "agent options must be a plain object"):
            scheduler.register_cached_agent(prompt="inspect repo", options="bad", result=result)

        self.assertEqual([], scheduler.jobs)

    def test_cache_key_changes_when_permission_profile_or_policy_version_changes(self):
        scheduler_a, _store_a, _run_a = self.make_scheduler()
        scheduler_b, _store_b, _run_b = self.make_scheduler(
            run_kwargs={
                "run_id": "wf_test_b",
                "permission_profile": "read_only",
                "permission_policy_version": "read-only-v1",
            }
        )

        job_a = scheduler_a.register_agent(prompt="same", options={"effort": "low"})
        job_b = scheduler_b.register_agent(prompt="same", options={"effort": "low"})

        self.assertNotEqual(job_a.metadata["cacheKey"], job_b.metadata["cacheKey"])
        self.assertEqual("inherit-current-permissions", job_a.metadata["permissionProfile"])
        self.assertEqual("read_only", job_b.metadata["permissionProfile"])
        self.assertEqual("inherit-current-v1", job_a.metadata["permissionPolicyVersion"])
        self.assertEqual("read-only-v1", job_b.metadata["permissionPolicyVersion"])

    def test_cache_key_args_hash_uses_runtime_args(self):
        scheduler_a, _store_a, _run_a = self.make_scheduler()
        scheduler_b, _store_b, _run_b = self.make_scheduler(run_kwargs={"run_id": "wf_test_b"})
        scheduler_a.args = {"target": "alpha"}
        scheduler_b.args = {"target": "beta"}

        job_a = scheduler_a.register_agent(prompt="same", options={"effort": "low"})
        job_b = scheduler_b.register_agent(prompt="same", options={"effort": "low"})

        self.assertNotEqual(job_a.metadata["cacheKey"]["argsHash"], job_b.metadata["cacheKey"]["argsHash"])
        self.assertNotEqual(job_a.metadata["cacheKey"], job_b.metadata["cacheKey"])

    def test_cache_key_distinguishes_json_values_from_json_like_strings(self):
        scheduler_object, _store_object, _run_object = self.make_scheduler()
        scheduler_string, _store_string, _run_string = self.make_scheduler(run_kwargs={"run_id": "wf_test_string"})
        scheduler_none, _store_none, _run_none = self.make_scheduler(run_kwargs={"run_id": "wf_test_none"})
        scheduler_null_string, _store_null_string, _run_null_string = self.make_scheduler(run_kwargs={"run_id": "wf_test_null_string"})
        scheduler_object.args = {}
        scheduler_string.args = "{}"
        scheduler_none.args = None
        scheduler_null_string.args = "null"

        object_job = scheduler_object.register_agent(prompt="same")
        string_job = scheduler_string.register_agent(prompt="same")
        none_job = scheduler_none.register_agent(prompt="same")
        null_string_job = scheduler_null_string.register_agent(prompt="same")

        self.assertNotEqual(object_job.metadata["cacheKey"]["argsHash"], string_job.metadata["cacheKey"]["argsHash"])
        self.assertNotEqual(none_job.metadata["cacheKey"]["argsHash"], null_string_job.metadata["cacheKey"]["argsHash"])

    def test_run_all_marks_cached_jobs_as_successful_completion(self):
        scheduler, store, run = self.make_scheduler()
        scheduler.register_cached_agent(
            prompt="cached work",
            result=AgentResult(job_id="agent_source", payload={"summary": "cached"}),
            source_run_id="wf_source",
            source_job_id="agent_1",
        )

        completed = scheduler.run_all()

        self.assertEqual([], completed)
        loaded = store.load_run(run.run_id)
        self.assertEqual("succeeded", loaded.status)
        self.assertEqual("cached", loaded.jobs[0].status)
        final_result = json.loads((Path(run.artifact_dir) / "final-result.json").read_text(encoding="utf-8"))
        self.assertEqual("succeeded", final_result["status"])
        self.assertEqual("cached", final_result["jobs"][0]["status"])

    def test_run_all_moves_successful_job_through_running_to_succeeded_and_writes_result(self):
        scheduler, store, run = self.make_scheduler(runner=FakeChildAgentRunner(results={"agent_1": {"summary": "ok"}}))
        job = scheduler.register_agent(prompt="do work")

        completed = scheduler.run_all()

        self.assertEqual([job], completed)
        self.assertEqual("succeeded", job.status)
        self.assertEqual({"summary": "ok"}, job.metadata["result"])
        self.assertIsNotNone(job.result_ref)
        self.assertEqual(["agent_registered", "agent_started", "agent_completed"], self.event_types(store))
        loaded = store.load_run(run.run_id)
        self.assertEqual("succeeded", loaded.jobs[0].status)
        self.assertEqual(job.result_ref, loaded.jobs[0].result_ref)

    def test_schema_fallback_records_workflow_issue_in_scheduler_artifacts(self):
        scheduler, store, run = self.make_scheduler(runner=FakeChildAgentRunner(results={"agent_1": {"summary": "plain text"}}))
        job = scheduler.register_agent(
            prompt="collect sources",
            label="collector",
            options={"schema": {"type": "object", "required": ["sources"]}, "fallback": "text"},
        )

        scheduler.run_all()

        loaded = store.load_run(run.run_id)
        self.assertEqual("succeeded", loaded.status)
        loaded_job = loaded.jobs[0]
        self.assertEqual("succeeded", loaded_job.status)
        self.assertTrue(loaded_job.metadata["schemaValidation"]["fallbackApplied"])
        self.assertTrue(loaded_job.metadata["result"]["schemaFallback"])
        self.assertEqual("schema_validation_failed", loaded.metadata["workflowIssues"][0]["code"])
        events = store.replay_events(run.run_id)
        self.assertIn("workflow_issue", [event.event_type for event in events])
        result_data = json.loads((Path(run.artifact_dir) / job.result_ref).read_text(encoding="utf-8"))
        self.assertTrue(result_data["payload"]["schemaFallback"])
        progress = json.loads((Path(run.artifact_dir) / "workflow-progress.json").read_text(encoding="utf-8"))
        self.assertEqual(loaded.metadata["workflowIssues"], progress["workflowIssues"])
        final_result = json.loads((Path(run.artifact_dir) / "final-result.json").read_text(encoding="utf-8"))
        self.assertEqual(loaded.metadata["workflowIssues"], final_result["workflowIssues"])

    def test_concurrency_limit_only_starts_configured_number_of_jobs_per_tick(self):
        scheduler, _store, _ = self.make_scheduler(max_concurrent=3, runner=FakeChildAgentRunner(delay_ticks=1))
        for index in range(20):
            scheduler.register_agent(prompt=f"job {index}")

        scheduler.tick()

        self.assertEqual(3, scheduler.running_count)
        self.assertEqual(17, scheduler.queued_count)
        statuses = [job.status for job in scheduler.jobs]
        self.assertEqual(3, statuses.count("running"))
        self.assertEqual(17, statuses.count("queued"))

    def test_scheduler_accepts_protocol_runner_and_persists_child_transcript_metadata(self):
        runner = ProtocolMetadataRunner()
        scheduler, store, run = self.make_scheduler(runner=runner)
        job = scheduler.register_agent(prompt="do protocol work")

        scheduler.run_all()

        self.assertEqual([job.job_id], runner.started)
        self.assertEqual("agents/agent_1/transcript.jsonl", job.metadata["transcriptRef"])
        self.assertEqual({"input_tokens": 5, "output_tokens": 7}, job.metadata["tokenUsage"])
        self.assertEqual({}, job.metadata["toolSummary"])
        transcript_path = Path(run.artifact_dir) / "agents" / "agent_1" / "transcript.jsonl"
        self.assertTrue(transcript_path.exists())
        transcript_lines = [json.loads(line) for line in transcript_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual("metadata", transcript_lines[0]["type"])
        self.assertEqual("assistant", transcript_lines[1]["type"])
        result_path = Path(run.artifact_dir) / job.result_ref
        result_data = json.loads(result_path.read_text(encoding="utf-8"))
        self.assertEqual("agents/agent_1/transcript.jsonl", result_data["transcriptRef"])
        self.assertEqual({"summary": "real-ish child result", "text": "verbose child transcript text"}, result_data["payload"])
        self.assertNotIn("transcriptEvents", result_data)
        completed_event = store.replay_events(run.run_id)[-1]
        self.assertEqual("agent_completed", completed_event.event_type)
        self.assertNotIn("transcriptEvents", completed_event.payload["result"])

    def test_permission_events_from_successful_agent_result_are_written_to_journal_before_completion(self):
        scheduler, store, run = self.make_scheduler(runner=PermissionEventsRunner())
        job = scheduler.register_agent(prompt="check permissions")

        scheduler.run_all()

        events = store.replay_events(run.run_id)
        self.assertEqual(
            ["agent_registered", "agent_started", "permission_profile_selected", "tool_allowed", "tool_denied", "agent_completed"],
            [event.event_type for event in events],
        )
        self.assertEqual(list(range(1, len(events) + 1)), [event.sequence for event in events])
        denied = events[4]
        self.assertEqual(job.job_id, denied.job_id)
        self.assertEqual("file_write", denied.payload["toolName"])
        self.assertEqual("read_only", denied.payload["profile"])
        self.assertEqual("deny", denied.payload["decision"])
        self.assertEqual("read_only_static_write_or_execute", denied.payload["reason"])
        self.assertEqual("deny", denied.payload["permission"]["action"])

    def test_permission_events_from_failed_agent_result_are_written_to_journal_before_failure(self):
        scheduler, store, run = self.make_scheduler(runner=PermissionEventsRunner(status="failed"))
        scheduler.register_agent(prompt="check permissions then fail")

        scheduler.run_all()

        events = store.replay_events(run.run_id)
        self.assertEqual("agent_failed", events[-1].event_type)
        self.assertEqual(
            ["permission_profile_selected", "tool_allowed", "tool_denied"],
            [event.event_type for event in events[2:5]],
        )
        self.assertEqual("file_write", events[4].payload["toolName"])

    def test_failed_agent_result_marks_job_failed_without_raising_or_killing_run(self):
        scheduler, store, run = self.make_scheduler(runner=FailedResultRunner())
        job = scheduler.register_agent(prompt="api may fail")

        scheduler.run_all(failure_policy="continue")

        self.assertEqual("failed", job.status)
        self.assertEqual("api down", job.error)
        self.assertEqual("running", run.status)
        self.assertEqual("agent_failed", self.event_types(store)[-1])
        failed_event = store.replay_events(run.run_id)[-1]
        self.assertNotIn("transcriptEvents", failed_event.payload["result"])
        transcript_path = Path(run.artifact_dir) / "agents" / "agent_1" / "transcript.jsonl"
        self.assertTrue(transcript_path.exists())

    def test_total_agents_cap_rejects_excess_job_and_records_event(self):
        scheduler, store, _ = self.make_scheduler(max_total=2)
        scheduler.register_agent(prompt="one")
        scheduler.register_agent(prompt="two")

        with self.assertRaises(RuntimeError):
            scheduler.register_agent(prompt="three")

        self.assertEqual(["agent_registered", "agent_registered", "agent_rejected"], self.event_types(store))
        rejected = store.replay_events("wf_test")[-1]
        self.assertEqual("max_total_exceeded", rejected.payload["reason"])
        self.assertEqual(2, len(scheduler.jobs))

    def test_continue_failure_policy_keeps_other_jobs_running(self):
        runner = FakeChildAgentRunner(fail_job_ids={"agent_1"}, delay_ticks=0)
        scheduler, store, _ = self.make_scheduler(max_concurrent=2, runner=runner)
        failed = scheduler.register_agent(prompt="fail")
        succeeded = scheduler.register_agent(prompt="succeed")

        scheduler.run_all(failure_policy="continue")

        self.assertEqual("failed", failed.status)
        self.assertEqual("succeeded", succeeded.status)
        self.assertEqual("running", scheduler.run.status)
        self.assertEqual(
            ["agent_registered", "agent_registered", "agent_started", "agent_started", "agent_failed", "agent_completed"],
            self.event_types(store),
        )

    def test_fail_fast_failure_policy_cancels_queued_and_running_jobs_and_fails_run(self):
        runner = FakeChildAgentRunner(fail_job_ids={"agent_1"}, delay_ticks=1)
        scheduler, store, _ = self.make_scheduler(max_concurrent=2, runner=runner)
        failed = scheduler.register_agent(prompt="fail")
        running_cancelled = scheduler.register_agent(prompt="running")
        queued_cancelled = scheduler.register_agent(prompt="queued")

        scheduler.run_all(failure_policy="fail_fast")

        self.assertEqual("failed", failed.status)
        self.assertEqual("cancelled", running_cancelled.status)
        self.assertEqual("cancelled", queued_cancelled.status)
        self.assertEqual("failed", scheduler.run.status)
        self.assertIn("agent_failed", self.event_types(store))
        self.assertEqual(2, self.event_types(store).count("agent_cancelled"))

    def test_stop_cancels_queued_jobs_and_requests_cancellation_for_running_jobs(self):
        runner = FakeChildAgentRunner(delay_ticks=2)
        scheduler, store, _ = self.make_scheduler(max_concurrent=2, runner=runner)
        running_one = scheduler.register_agent(prompt="one")
        running_two = scheduler.register_agent(prompt="two")
        queued = scheduler.register_agent(prompt="three")
        scheduler.tick()

        scheduler.stop(reason="user stop")
        scheduler.run_all()

        self.assertEqual("cancelled", running_one.status)
        self.assertEqual("cancelled", running_two.status)
        self.assertEqual("cancelled", queued.status)
        self.assertEqual("killed", scheduler.run.status)
        self.assertEqual({"agent_1", "agent_2"}, runner.cancelled_job_ids)
        self.assertEqual(3, self.event_types(store).count("agent_cancelled"))


if __name__ == "__main__":
    unittest.main()
