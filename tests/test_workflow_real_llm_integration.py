import os
import time
import unittest

from workflow_child_agent import NativeGPTChildAgentRunner
from workflow_models import WorkflowJob


@unittest.skipUnless(os.environ.get("GA_RUN_REAL_LLM_TESTS") == "1", "set GA_RUN_REAL_LLM_TESTS=1 to run real LLM integration tests")
class WorkflowRealLLMIntegrationTest(unittest.TestCase):
    def wait_for_result(self, runner, job, timeout=60.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            result = runner.poll(job)
            if result is not None:
                return result
            time.sleep(0.1)
        self.fail("real LLM child runner did not finish in time")

    def test_native_oai_child_runner_completes_short_prompt(self):
        from llmcore import resolve_session

        runner = NativeGPTChildAgentRunner(
            config_name="native_oai_config",
            session_factory=resolve_session,
            max_tokens=64,
        )
        job = WorkflowJob(
            job_id="agent_real_1",
            prompt="Reply with the exact word OK and no other text.",
            phase="P3",
            metadata={"runId": "wf_real_llm_test", "label": "real-native-oai"},
        )

        runner.start(job)
        result = self.wait_for_result(runner, job)

        self.assertEqual("succeeded", result.status, result.payload.get("error"))
        self.assertTrue(result.payload.get("text", "").strip())
        self.assertEqual("agents/agent_real_1/transcript.jsonl", result.transcript_ref)
        self.assertTrue(any(event.get("type") == "request" for event in result.transcript_events))


if __name__ == "__main__":
    unittest.main()
