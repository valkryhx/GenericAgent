import json
import queue
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTENDS = REPO_ROOT / "frontends"
if str(FRONTENDS) not in sys.path:
    sys.path.insert(0, str(FRONTENDS))


from ink_bridge import GenericAgentBridge  # noqa: E402
from workflow_child_agent import AgentResult  # noqa: E402
from workflow_runtime import WorkflowRuntime  # noqa: E402
from workflow_scheduler import SchedulerConfig  # noqa: E402
from workflow_store import WorkflowStore  # noqa: E402
from workflow_models import WorkflowRun  # noqa: E402


class FakeBackend:
    def __init__(self):
        self.history = []
        self.last_usage_tokens = None


class FakeClient:
    def __init__(self):
        self.backend = FakeBackend()
        self.last_tools = ""


class FakeAgent:
    def __init__(self):
        self.inc_out = False
        self.verbose = True
        self.is_running = False
        self.aborted = False
        self.prompts = []
        self.queues = []
        self.history = []
        self.handler = object()
        self.llmclient = FakeClient()
        self.llmclients = [self.llmclient]
        self.llm_no = 0
        self.session_id = "session_provider_anomaly"

    def run(self):
        return None

    def put_task(self, text, source="user"):
        self.prompts.append((text, source))
        dq = queue.Queue()
        self.queues.append(dq)
        return dq

    def abort(self):
        self.aborted = True


class ProviderAnomalyRunner:
    def __init__(self, results):
        self.results = list(results)
        self.started_job_ids = []
        self.cancelled_job_ids = []

    def start(self, job):
        self.started_job_ids.append(job.job_id)

    def poll(self, job):
        if not self.results:
            return None
        result = self.results.pop(0)
        result.job_id = job.job_id
        return result

    def cancel(self, job):
        self.cancelled_job_ids.append(job.job_id)


def make_result(job_id, status, payload, *, token_usage=None, tool_summary=None, transcript_events=None):
    return AgentResult(
        job_id=job_id,
        status=status,
        payload=payload,
        transcript_ref=f"agents/{job_id}/transcript.jsonl",
        token_usage=token_usage or {},
        tool_summary=tool_summary or {},
        transcript_events=transcript_events or [],
    )


def transcript_success(text):
    return [
        {"type": "metadata", "runner": "p8_provider_anomaly_fake"},
        {"type": "request", "content": "call provider"},
        {"type": "assistant", "text": text},
    ]


def transcript_failure(error):
    return [
        {"type": "metadata", "runner": "p8_provider_anomaly_fake"},
        {"type": "request", "content": "call provider"},
        {"type": "error", "error": error},
    ]


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def read_jsonl(path):
    rows = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        if line.strip():
            rows.append(json.loads(line))
    return rows


class P8ProviderAnomalyE2ETest(unittest.TestCase):
    def run_runtime_case(self, result):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            run = store.create_run(
                WorkflowRun(
                    run_id="wf_provider_anomaly",
                    session_id="session_provider_anomaly",
                    script="return await agent('provider anomaly probe')",
                    status="running",
                )
            )
            runner = ProviderAnomalyRunner([result])
            runtime = WorkflowRuntime(
                store=store,
                runner=runner,
                scheduler_config=SchedulerConfig(max_concurrent=1, max_total=2),
                timeout_seconds=2.0,
            )
            return tmp, store, run, runner, runtime

    def assert_external_transcript(self, artifact_dir, result_json):
        self.assertIn("transcriptRef", result_json)
        self.assertNotIn("transcriptEvents", result_json)
        transcript_path = Path(artifact_dir) / result_json["transcriptRef"]
        self.assertTrue(transcript_path.exists())
        rows = read_jsonl(transcript_path)
        self.assertTrue(rows)
        self.assertTrue(all(isinstance(row, dict) for row in rows))
        return rows

    def assert_bridge_idle(self, events):
        self.assertEqual({"type": "activity", "label": None}, events[-2])
        self.assertEqual({"type": "status", "status": "idle"}, events[-1])

    def make_bridge(self, tmp, runner, events):
        def runtime_factory(*, store, timeout_seconds=10.0):
            return WorkflowRuntime(
                store=store,
                runner=runner,
                scheduler_config=SchedulerConfig(max_concurrent=1, max_total=2),
                timeout_seconds=timeout_seconds,
            )

        return GenericAgentBridge(
            agent_factory=FakeAgent,
            emit=events.append,
            workflow_root=tmp,
            workflow_runtime_factory=runtime_factory,
        )

    def test_runtime_provider_empty_content_preserves_success_artifact_and_readable_transcript(self):
        result = make_result(
            "agent_1",
            "succeeded",
            {"summary": "", "text": ""},
            transcript_events=transcript_success(""),
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            run = store.create_run(WorkflowRun(run_id="wf_provider_anomaly", session_id="session_provider_anomaly", script="return await agent('empty')", status="running"))
            runtime = WorkflowRuntime(store=store, runner=ProviderAnomalyRunner([result]), timeout_seconds=2.0)

            runtime.run(run)

            loaded = store.load_run(run.run_id)
            self.assertEqual("succeeded", loaded.status)
            job = loaded.jobs[0]
            artifact_dir = Path(loaded.artifact_dir)
            result_json = read_json(artifact_dir / job.result_ref)
            self.assertEqual("succeeded", result_json["status"])
            self.assertEqual({"summary": "", "text": ""}, result_json["payload"])
            self.assertEqual({}, result_json["tokenUsage"])
            rows = self.assert_external_transcript(artifact_dir, result_json)
            self.assertTrue(any(row.get("type") == "assistant" and row.get("text") == "" for row in rows))
            final_json = read_json(artifact_dir / "final-result.json")
            self.assertEqual("succeeded", final_json["status"])

    def test_runtime_provider_missing_usage_does_not_fail_and_records_empty_token_usage(self):
        result = make_result(
            "agent_1",
            "succeeded",
            {"summary": "ok", "text": "ok"},
            token_usage={},
            transcript_events=transcript_success("ok"),
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            run = store.create_run(WorkflowRun(run_id="wf_provider_anomaly", session_id="session_provider_anomaly", script="return await agent('missing usage')", status="running"))
            runtime = WorkflowRuntime(store=store, runner=ProviderAnomalyRunner([result]), timeout_seconds=2.0)

            runtime.run(run)

            loaded = store.load_run(run.run_id)
            self.assertEqual("succeeded", loaded.status)
            artifact_dir = Path(loaded.artifact_dir)
            result_json = read_json(artifact_dir / loaded.jobs[0].result_ref)
            self.assertEqual("succeeded", result_json["status"])
            self.assertEqual({}, result_json["tokenUsage"])
            rows = self.assert_external_transcript(artifact_dir, result_json)
            self.assertFalse(any(row.get("type") == "token_usage" for row in rows))
            final_json = read_json(artifact_dir / "final-result.json")
            self.assertEqual("succeeded", final_json["status"])

    def test_runtime_provider_no_text_block_preserves_success_artifact_without_inline_transcript(self):
        result = make_result(
            "agent_1",
            "succeeded",
            {"summary": "", "text": "", "providerAnomaly": "no_text_blocks"},
            transcript_events=[
                {"type": "metadata", "runner": "p8_provider_anomaly_fake"},
                {"type": "request", "content": "call provider"},
                {"type": "provider_anomaly", "kind": "no_text_blocks", "blockTypes": ["thinking"]},
                {"type": "assistant", "text": ""},
            ],
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            run = store.create_run(WorkflowRun(run_id="wf_provider_anomaly", session_id="session_provider_anomaly", script="return await agent('no text')", status="running"))
            runtime = WorkflowRuntime(store=store, runner=ProviderAnomalyRunner([result]), timeout_seconds=2.0)

            runtime.run(run)

            loaded = store.load_run(run.run_id)
            self.assertEqual("succeeded", loaded.status)
            artifact_dir = Path(loaded.artifact_dir)
            result_json = read_json(artifact_dir / loaded.jobs[0].result_ref)
            self.assertEqual("succeeded", result_json["status"])
            self.assertEqual("no_text_blocks", result_json["payload"]["providerAnomaly"])
            rows = self.assert_external_transcript(artifact_dir, result_json)
            self.assertTrue(any(row.get("type") == "provider_anomaly" and row.get("kind") == "no_text_blocks" for row in rows))
            final_json = read_json(artifact_dir / "final-result.json")
            self.assertEqual("succeeded", final_json["status"])

    def test_runtime_provider_sdk_like_exception_preserves_failed_result_transcript_and_final_failure(self):
        error = "provider sdk error: HTTP 503 upstream unavailable"
        result = make_result(
            "agent_1",
            "failed",
            {"error": error},
            transcript_events=transcript_failure(error),
        )
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            run = store.create_run(WorkflowRun(run_id="wf_provider_anomaly", session_id="session_provider_anomaly", script="return await agent('sdk error')", status="running"))
            runtime = WorkflowRuntime(store=store, runner=ProviderAnomalyRunner([result]), timeout_seconds=2.0)

            with self.assertRaisesRegex(RuntimeError, "HTTP 503"):
                runtime.run(run)

            loaded = store.load_run(run.run_id)
            self.assertEqual("failed", loaded.status)
            artifact_dir = Path(loaded.artifact_dir)
            result_json = read_json(artifact_dir / loaded.jobs[0].result_ref)
            self.assertEqual("failed", result_json["status"])
            self.assertIn("HTTP 503", result_json["payload"]["error"])
            rows = self.assert_external_transcript(artifact_dir, result_json)
            self.assertTrue(any(row.get("type") == "error" and "HTTP 503" in row.get("error", "") for row in rows))
            final_json = read_json(artifact_dir / "final-result.json")
            self.assertEqual("failed", final_json["status"])
            self.assertIn("HTTP 503", final_json["error"])
            event_types = [event.event_type for event in store.replay_events(run.run_id)]
            self.assertIn("agent_failed", event_types)
            self.assertEqual("workflow_failed", event_types[-1])

    def test_bridge_provider_empty_content_emits_success_final_and_idle(self):
        result = make_result("agent_1", "succeeded", {"summary": "", "text": ""}, transcript_events=transcript_success(""))
        events = []
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self.make_bridge(tmp, ProviderAnomalyRunner([result]), events)
            run_id = bridge.workflow_draft("return await agent('empty')")
            self.assertTrue(bridge.workflow_approve(run_id, timeout_seconds=2.0))
            bridge.wait_for_workflow_idle(run_id, timeout=5)

            final = next(event for event in events if event["type"] == "workflow_final" and event["runId"] == run_id)
            self.assertEqual("succeeded", final["result"]["status"])
            self.assertFalse(any(event.get("type") == "error" and event.get("code") == "workflow_run_failed" for event in events))
            self.assert_bridge_idle(events)

    def test_bridge_provider_missing_usage_emits_success_final_without_workflow_error(self):
        result = make_result("agent_1", "succeeded", {"summary": "ok", "text": "ok"}, token_usage={}, transcript_events=transcript_success("ok"))
        events = []
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self.make_bridge(tmp, ProviderAnomalyRunner([result]), events)
            run_id = bridge.workflow_draft("return await agent('missing usage')")
            self.assertTrue(bridge.workflow_approve(run_id, timeout_seconds=2.0))
            bridge.wait_for_workflow_idle(run_id, timeout=5)

            run = bridge.workflow_store.load_run(run_id)
            result_json = read_json(Path(run.artifact_dir) / run.jobs[0].result_ref)
            self.assertEqual({}, result_json["tokenUsage"])
            final = next(event for event in events if event["type"] == "workflow_final" and event["runId"] == run_id)
            self.assertEqual("succeeded", final["result"]["status"])
            self.assertFalse(any(event.get("type") == "error" and event.get("code") == "workflow_run_failed" for event in events))
            self.assert_bridge_idle(events)

    def test_bridge_provider_sdk_like_exception_emits_failed_final_error_and_idle(self):
        error = "provider sdk error: HTTP 503 upstream unavailable"
        result = make_result("agent_1", "failed", {"error": error}, transcript_events=transcript_failure(error))
        events = []
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self.make_bridge(tmp, ProviderAnomalyRunner([result]), events)
            run_id = bridge.workflow_draft("return await agent('sdk error')")
            self.assertTrue(bridge.workflow_approve(run_id, timeout_seconds=2.0))
            bridge.wait_for_workflow_idle(run_id, timeout=5)

            run = bridge.workflow_store.load_run(run_id)
            artifact_dir = Path(run.artifact_dir)
            result_json = read_json(artifact_dir / run.jobs[0].result_ref)
            self.assertEqual("failed", result_json["status"])
            self.assert_external_transcript(artifact_dir, result_json)
            final = next(event for event in events if event["type"] == "workflow_final" and event["runId"] == run_id)
            self.assertEqual("failed", final["result"]["status"])
            self.assertIn("HTTP 503", final["result"]["error"])
            error_event = next(event for event in events if event.get("type") == "error" and event.get("code") == "workflow_run_failed")
            self.assertIn("HTTP 503", error_event["message"])
            self.assert_bridge_idle(events)

    def test_bridge_provider_no_text_block_emits_success_final_and_preserves_readable_transcript(self):
        result = make_result(
            "agent_1",
            "succeeded",
            {"summary": "", "text": "", "providerAnomaly": "no_text_blocks"},
            transcript_events=[
                {"type": "metadata", "runner": "p8_provider_anomaly_fake"},
                {"type": "provider_anomaly", "kind": "no_text_blocks", "blockTypes": ["thinking"]},
                {"type": "assistant", "text": ""},
            ],
        )
        events = []
        with tempfile.TemporaryDirectory() as tmp:
            bridge = self.make_bridge(tmp, ProviderAnomalyRunner([result]), events)
            run_id = bridge.workflow_draft("return await agent('no text')")
            self.assertTrue(bridge.workflow_approve(run_id, timeout_seconds=2.0))
            bridge.wait_for_workflow_idle(run_id, timeout=5)

            run = bridge.workflow_store.load_run(run_id)
            result_json = read_json(Path(run.artifact_dir) / run.jobs[0].result_ref)
            rows = self.assert_external_transcript(run.artifact_dir, result_json)
            self.assertTrue(any(row.get("type") == "provider_anomaly" for row in rows))
            final = next(event for event in events if event["type"] == "workflow_final" and event["runId"] == run_id)
            self.assertEqual("succeeded", final["result"]["status"])
            self.assertFalse(any(event.get("type") == "error" and event.get("code") == "workflow_run_failed" for event in events))
            self.assert_bridge_idle(events)


if __name__ == "__main__":
    unittest.main()
