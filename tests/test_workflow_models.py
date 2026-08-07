import unittest

from workflow_models import (
    AgentResult,
    DEFAULT_PERMISSION_POLICY_VERSION,
    DEFAULT_PERMISSION_PROFILE,
    JOB_STATUSES,
    RUN_STATUSES,
    WorkflowEvent,
    WorkflowJob,
    WorkflowRun,
    new_run_id,
    project_workflow_execution_outcome,
    summarize_workflow_jobs,
)


class WorkflowModelsTest(unittest.TestCase):
    def test_defaults_use_inherited_current_permissions(self):
        run = WorkflowRun(session_id="session_test", script="log('hello')")

        self.assertEqual("draft", run.status)
        self.assertTrue(run.run_id.startswith("wf_"))
        self.assertEqual(DEFAULT_PERMISSION_PROFILE, run.permission_profile)
        self.assertEqual("inherit-current-permissions", run.permission_profile)
        self.assertEqual(DEFAULT_PERMISSION_POLICY_VERSION, run.permission_policy_version)
        self.assertEqual("inherit-current-v1", run.permission_policy_version)

    def test_status_sets_include_required_lifecycle_values(self):
        self.assertTrue(
            {
                "draft",
                "awaiting_approval",
                "running",
                "succeeded",
                "failed",
                "cancelled",
                "killed",
                "interrupted",
            }.issubset(RUN_STATUSES)
        )
        self.assertTrue(
            {
                "registered",
                "queued",
                "running",
                "succeeded",
                "failed",
                "cancelled",
                "killed",
                "cached",
                "skipped",
                "stale",
            }.issubset(JOB_STATUSES)
        )

    def test_run_round_trips_to_dict_without_losing_permissions_or_jobs(self):
        run = WorkflowRun(
            run_id="wf_test",
            session_id="session_test",
            script="phase('Scan')",
            status="awaiting_approval",
            artifact_dir="C:/tmp/session_test/workflows/wf_test",
            jobs=[
                WorkflowJob(
                    job_id="agent_1",
                    prompt="inspect files",
                    status="queued",
                    phase="Scan",
                    result_ref="agents/agent_1/result.json",
                )
            ],
        )

        restored = WorkflowRun.from_dict(run.to_dict())

        self.assertEqual(run, restored)
        data = restored.to_dict()
        self.assertEqual("inherit-current-permissions", data["permissionProfile"])
        self.assertEqual("inherit-current-v1", data["permissionPolicyVersion"])
        self.assertEqual("agent_1", data["jobs"][0]["jobId"])

    def test_event_requires_core_fields_and_round_trips_camel_case(self):
        event = WorkflowEvent(
            run_id="wf_test",
            event_type="workflow_started",
            sequence=3,
            session_id="session_test",
            payload={"artifactDir": "temp/sessions/session_test/workflows/wf_test"},
        )

        data = event.to_dict()
        restored = WorkflowEvent.from_dict(data)

        self.assertEqual(event, restored)
        self.assertEqual(1, data["version"])
        self.assertEqual("workflow_started", data["type"])
        self.assertEqual("wf_test", data["runId"])
        self.assertEqual(3, data["sequence"])
        self.assertEqual({"artifactDir": "temp/sessions/session_test/workflows/wf_test"}, data["payload"])

    def test_agent_result_round_trips_compact_payload(self):
        result = AgentResult(
            job_id="agent_1",
            status="succeeded",
            payload={"summary": "ok"},
            transcript_ref="agents/agent_1/transcript.jsonl",
            token_usage={"input": 1, "output": 2},
            tool_summary={"Read": 3},
        )

        data = result.to_dict()
        restored = AgentResult.from_dict(data)

        self.assertEqual(result, restored)
        self.assertEqual("agent_1", data["jobId"])
        self.assertEqual({"summary": "ok"}, data["payload"])
        self.assertEqual("agents/agent_1/transcript.jsonl", data["transcriptRef"])

    def test_new_run_id_uses_workflow_prefix(self):
        self.assertTrue(new_run_id().startswith("wf_"))

    def test_workflow_child_summary_projects_handled_failure_to_partial(self):
        jobs = [
            WorkflowJob(job_id="agent_1", status="succeeded"),
            WorkflowJob(job_id="agent_2", status="failed"),
            WorkflowJob(
                job_id="agent_3",
                status="cached",
                metadata={"cachedFromRunId": "wf_source", "cachedFromJobId": "agent_3"},
            ),
        ]

        summary = summarize_workflow_jobs(jobs)

        self.assertEqual(3, summary["total"])
        self.assertEqual(1, summary["succeeded"])
        self.assertEqual(1, summary["failed"])
        self.assertEqual(1, summary["cached"])
        self.assertEqual(3, summary["terminal"])
        self.assertEqual("partial", project_workflow_execution_outcome("succeeded", summary))
        self.assertEqual("failed", project_workflow_execution_outcome("failed", summary))


if __name__ == "__main__":
    unittest.main()
