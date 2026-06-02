import time
import unittest

from workflow_child_agent import NativeGPTChildAgentRunner
from workflow_models import WorkflowJob


class StubSession:
    def __init__(self, chunks=("stub ", "answer"), error=None, usage=None, delay=0):
        self.chunks = chunks
        self.error = error
        self.last_usage_tokens = usage if usage is not None else {"input_tokens": 3, "output_tokens": 2}
        self.delay = delay
        self.history = []
        self.prompts = []
        self.messages = []
        self.cancelled = False
        self.max_tokens = None
        self.system = ""

    def ask(self, message):
        self.messages.append(message)
        self.assert_native_message(message)
        prompt = message["content"][0]["text"]
        self.prompts.append(prompt)
        self.history.append(message)
        if self.delay:
            time.sleep(self.delay)
        if self.error:
            raise self.error
        def gen():
            text = ""
            for chunk in self.chunks:
                text += chunk
                yield chunk
            self.history.append({"role": "assistant", "content": [{"type": "text", "text": text}]})
        return gen()

    def assert_native_message(self, message):
        if not isinstance(message, dict):
            raise AssertionError(f"expected dict message, got {type(message).__name__}")
        if message.get("role") != "user":
            raise AssertionError(f"expected user role, got {message.get('role')!r}")
        content = message.get("content")
        if not isinstance(content, list) or len(content) != 1:
            raise AssertionError(f"expected one content block, got {content!r}")
        block = content[0]
        if block.get("type") != "text" or not isinstance(block.get("text"), str):
            raise AssertionError(f"expected text block, got {block!r}")

    def cancel_current_request(self):
        self.cancelled = True


class NativeGPTChildAgentRunnerTest(unittest.TestCase):
    def wait_for_result(self, runner, job, timeout=2.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            result = runner.poll(job)
            if result is not None:
                return result
            time.sleep(0.01)
        self.fail("runner did not finish in time")

    def test_native_runner_uses_injected_independent_session_and_returns_metadata_rich_result(self):
        parent_history = [{"role": "user", "content": "parent only"}]
        created = []
        def factory(config_name):
            self.assertEqual("native_oai_config", config_name)
            session = StubSession()
            created.append(session)
            return session
        job = WorkflowJob(
            job_id="agent_1",
            prompt="summarize the repository",
            phase="P3",
            metadata={
                "runId": "wf_test",
                "label": "Scout",
                "options": {"effort": "low"},
                "parentHistory": parent_history,
            },
        )
        runner = NativeGPTChildAgentRunner(
            session_factory=factory,
            system_prompt="child system prompt",
            max_tokens=12,
        )

        runner.start(job)
        result = self.wait_for_result(runner, job)

        self.assertEqual("succeeded", result.status)
        self.assertEqual("agent_1", result.job_id)
        self.assertEqual("stub answer", result.payload["summary"])
        self.assertEqual("stub answer", result.payload["text"])
        self.assertEqual("agents/agent_1/transcript.jsonl", result.transcript_ref)
        self.assertEqual({"input_tokens": 3, "output_tokens": 2}, result.token_usage)
        self.assertEqual({}, result.tool_summary)
        self.assertGreaterEqual(len(result.transcript_events), 3)
        prompt = created[0].prompts[0]
        self.assertIn("runId: wf_test", prompt)
        self.assertIn("jobId: agent_1", prompt)
        self.assertIn("phase: P3", prompt)
        self.assertIn("label: Scout", prompt)
        self.assertIn("summarize the repository", prompt)
        self.assertEqual(parent_history, job.metadata["parentHistory"])
        self.assertIsNot(parent_history, created[0].history)
        self.assertEqual(12, created[0].max_tokens)

    def test_native_runner_reports_api_errors_as_failed_results_without_raising_from_poll(self):
        job = WorkflowJob(job_id="agent_1", prompt="fail please", metadata={"runId": "wf_test"})
        runner = NativeGPTChildAgentRunner(session_factory=lambda config_name: StubSession(error=RuntimeError("api down")))

        runner.start(job)
        result = self.wait_for_result(runner, job)

        self.assertEqual("failed", result.status)
        self.assertIn("api down", result.payload["error"])
        self.assertEqual("agents/agent_1/transcript.jsonl", result.transcript_ref)
        self.assertTrue(any(event.get("type") == "error" for event in result.transcript_events))

    def test_cancel_requests_active_session_cancellation(self):
        session = StubSession(delay=0.2)
        job = WorkflowJob(job_id="agent_1", prompt="slow", metadata={"runId": "wf_test"})
        runner = NativeGPTChildAgentRunner(session_factory=lambda config_name: session)

        runner.start(job)
        runner.cancel(job)

        self.assertTrue(session.cancelled)

    def test_start_creates_a_fresh_session_for_each_job(self):
        created = []
        def factory(config_name):
            session = StubSession()
            created.append(session)
            return session
        runner = NativeGPTChildAgentRunner(session_factory=factory)
        first = WorkflowJob(job_id="agent_1", prompt="one", metadata={"runId": "wf_test"})
        second = WorkflowJob(job_id="agent_2", prompt="two", metadata={"runId": "wf_test"})

        runner.start(first)
        runner.start(second)
        self.wait_for_result(runner, first)
        self.wait_for_result(runner, second)

        self.assertEqual(2, len(created))
        self.assertIsNot(created[0], created[1])
        self.assertEqual(2, sum(len(session.prompts) for session in created))


if __name__ == "__main__":
    unittest.main()
