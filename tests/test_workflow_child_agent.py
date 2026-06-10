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


class StubToolFunction:
    def __init__(self, name, arguments):
        self.name = name
        self.arguments = arguments


class StubToolCall:
    def __init__(self, name, arguments, id="tool_1"):
        import json
        self.function = StubToolFunction(name, json.dumps(arguments))
        self.id = id


class StubToolResponse:
    def __init__(self, content, tool_calls=None):
        self.thinking = ""
        self.content = content
        self.tool_calls = tool_calls or []
        self.raw = content
        self.stop_reason = "tool_use" if self.tool_calls else "end_turn"


class StubToolClient:
    def __init__(self, responses, usage=None):
        self.responses = list(responses)
        self.requests = []
        self.tools_seen = []
        self.last_usage_tokens = usage if usage is not None else {"input_tokens": 5, "output_tokens": 4}
        self.cancelled = False
        self.last_tools = ""

    def chat(self, messages, tools=None):
        self.requests.append(messages)
        self.tools_seen.append(tools)
        if not self.responses:
            raise AssertionError("no stub response available")
        response = self.responses.pop(0)
        def gen():
            if response.content:
                yield response.content
            return response
        return gen()

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

    def test_child_prompt_or_metadata_carries_permission_profile(self):
        created = []
        def factory(config_name):
            session = StubSession()
            created.append(session)
            return session
        job = WorkflowJob(
            job_id="agent_1",
            prompt="inspect permissions",
            metadata={
                "runId": "wf_test",
                "permissionProfile": "read_only",
                "permissionPolicyVersion": "read-only-v1",
            },
        )
        runner = NativeGPTChildAgentRunner(session_factory=factory)

        runner.start(job)
        result = self.wait_for_result(runner, job)

        prompt = created[0].prompts[0]
        self.assertIn("permissionProfile: read_only", prompt)
        self.assertIn("permissionPolicyVersion: read-only-v1", prompt)
        metadata_event = result.transcript_events[0]
        self.assertEqual("metadata", metadata_event["type"])
        self.assertEqual("read_only", metadata_event["permissionProfile"])
        self.assertEqual("read-only-v1", metadata_event["permissionPolicyVersion"])

    def test_native_runner_reports_api_errors_as_failed_results_without_raising_from_poll(self):
        job = WorkflowJob(job_id="agent_1", prompt="fail please", metadata={"runId": "wf_test"})
        runner = NativeGPTChildAgentRunner(session_factory=lambda config_name: StubSession(error=RuntimeError("api down")))

        runner.start(job)
        result = self.wait_for_result(runner, job)

        self.assertEqual("failed", result.status)
        self.assertIn("api down", result.payload["error"])
        self.assertEqual("agents/agent_1/transcript.jsonl", result.transcript_ref)
        self.assertTrue(any(event.get("type") == "error" for event in result.transcript_events))

    def test_native_runner_empty_content_succeeds_with_empty_summary_and_readable_transcript(self):
        job = WorkflowJob(job_id="agent_1", prompt="empty please", metadata={"runId": "wf_test"})
        runner = NativeGPTChildAgentRunner(session_factory=lambda config_name: StubSession(chunks=(), usage={}))

        runner.start(job)
        result = self.wait_for_result(runner, job)

        self.assertEqual("succeeded", result.status)
        self.assertEqual("", result.payload["summary"])
        self.assertEqual("", result.payload["text"])
        self.assertEqual({}, result.token_usage)
        self.assertEqual("agents/agent_1/transcript.jsonl", result.transcript_ref)
        self.assertTrue(any(event.get("type") == "assistant" and event.get("text") == "" for event in result.transcript_events))
        self.assertFalse(any(event.get("type") == "token_usage" for event in result.transcript_events))

    def test_native_runner_missing_usage_omits_token_usage_but_preserves_text(self):
        job = WorkflowJob(job_id="agent_1", prompt="usage missing", metadata={"runId": "wf_test"})
        runner = NativeGPTChildAgentRunner(session_factory=lambda config_name: StubSession(chunks=("answer",), usage={}))

        runner.start(job)
        result = self.wait_for_result(runner, job)

        self.assertEqual("succeeded", result.status)
        self.assertEqual("answer", result.payload["summary"])
        self.assertEqual({}, result.token_usage)
        self.assertFalse(any(event.get("type") == "token_usage" for event in result.transcript_events))

    def test_success_transcript_redacts_sensitive_metadata_options_and_request_prompt(self):
        secret_values = ["bearer-option-secret", "prompt-secret"]
        job = WorkflowJob(
            job_id="agent_1",
            prompt="inspect Authorization: Bearer prompt-secret request_id=req_123",
            metadata={
                "runId": "wf_test",
                "options": {
                    "apiKey": "option-secret",
                    "note": "Bearer bearer-option-secret request_id=req_123",
                },
            },
        )
        runner = NativeGPTChildAgentRunner(session_factory=lambda config_name: StubSession())

        runner.start(job)
        result = self.wait_for_result(runner, job)

        serialized = repr(result.transcript_events)
        self.assertEqual("succeeded", result.status)
        self.assertIn("request_id=req_123", serialized)
        self.assertIn("[REDACTED]", serialized)
        for secret in secret_values:
            self.assertNotIn(secret, serialized)

    def test_native_tool_runner_redacts_sensitive_tool_call_args_and_results_in_success_transcript(self):
        from agent_loop import StepOutcome
        from unittest import mock

        secret_values = ["tool-secret", "bearer-tool-secret", "result-secret", "cookie_secret"]
        client = StubToolClient([
            StubToolResponse("<summary>use tool</summary>", [
                StubToolCall(
                    "file_read",
                    {
                        "path": __file__,
                        "api_key": "tool-secret",
                        "note": "Bearer bearer-tool-secret request_id=req_123",
                    },
                )
            ]),
            StubToolResponse("<summary>done</summary>tool complete"),
        ])
        tools = [
            {"type": "function", "function": {"name": "file_read", "parameters": {"type": "object", "properties": {}}}},
        ]
        job = WorkflowJob(job_id="agent_tool_redact", prompt="read safely", metadata={"runId": "wf_test"})
        runner = NativeGPTChildAgentRunner(client_factory=lambda config_name: client, tools_schema_factory=lambda: tools)

        with mock.patch("ga.GenericAgentHandler.do_file_read", return_value=StepOutcome({"status": "success", "content": "token=result-secret Cookie: sid=cookie_secret request_id=req_123"})):
            runner.start(job)
            result = self.wait_for_result(runner, job)

        serialized = repr(result.transcript_events)
        self.assertEqual("succeeded", result.status)
        self.assertIn("request_id=req_123", serialized)
        self.assertIn("[REDACTED]", serialized)
        for secret in secret_values:
            self.assertNotIn(secret, serialized)
        self.assertTrue(any(event.get("type") == "tool_call" for event in result.transcript_events))
        self.assertTrue(any(event.get("type") == "tool_result" for event in result.transcript_events))

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

    def test_native_tool_runner_allows_file_read_through_generic_handler_dispatch(self):
        client = StubToolClient([
            StubToolResponse("<summary>need read</summary>", [StubToolCall("file_read", {"path": __file__, "show_linenos": False})]),
            StubToolResponse("<summary>done</summary>read complete"),
        ])
        job = WorkflowJob(
            job_id="agent_tool_read",
            prompt="read test file",
            metadata={"runId": "wf_test", "permissionProfile": "inherit-current-permissions", "permissionPolicyVersion": "inherit-current-v1"},
        )
        tools = [
            {"type": "function", "function": {"name": "file_read", "parameters": {"type": "object", "properties": {}}}},
        ]
        runner = NativeGPTChildAgentRunner(client_factory=lambda config_name: client, tools_schema_factory=lambda: tools)

        runner.start(job)
        result = self.wait_for_result(runner, job)

        self.assertEqual("succeeded", result.status)
        self.assertIn("read complete", result.payload["text"])
        self.assertEqual({"input_tokens": 5, "output_tokens": 4}, result.token_usage)
        self.assertTrue(client.tools_seen and client.tools_seen[0] == tools)
        self.assertTrue(any(event.get("type") == "tool_call" and event.get("toolName") == "file_read" for event in result.transcript_events))
        self.assertTrue(any(event.get("type") == "tool_result" and event.get("toolName") == "file_read" for event in result.transcript_events))
        self.assertEqual(["file_read", "no_tool"], result.tool_summary["allowedTools"])
        self.assertEqual(0, result.tool_summary["denied"])

    def test_native_tool_runner_loads_skill_and_mcp_through_generic_handler_dispatch(self):
        import os
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as td:
            skill_dir = os.path.join(td, "sample-skill")
            os.makedirs(skill_dir)
            with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as f:
                f.write("---\nname: sample-skill\ndescription: sample skill\nallowed-tools: [file_read]\n---\n# Sample skill\n")
            client = StubToolClient([
                StubToolResponse("<summary>load skill and mcp</summary>", [
                    StubToolCall("load_skill", {"skill": "sample-skill", "search_roots": [td]}, id="tool_skill"),
                    StubToolCall("mcp__deterministic__read_marker", {"marker": "ok"}, id="tool_mcp"),
                ]),
                StubToolResponse("<summary>done</summary>skill and mcp complete"),
            ])
            tools = [
                {"type": "function", "function": {"name": "load_skill", "parameters": {"type": "object", "properties": {}}}},
                {"type": "function", "function": {"name": "mcp__deterministic__read_marker", "parameters": {"type": "object", "properties": {}}}},
            ]
            job = WorkflowJob(job_id="agent_tool_mcp", prompt="load skill and read marker", metadata={"runId": "wf_test"})
            runner = NativeGPTChildAgentRunner(client_factory=lambda config_name: client, tools_schema_factory=lambda: tools)

            with mock.patch("mcp_runtime.call_mcp_tool", return_value={"status": "success", "marker": "ok"}) as call_mcp:
                runner.start(job)
                result = self.wait_for_result(runner, job)

            self.assertEqual("succeeded", result.status)
            call_mcp.assert_called_once_with("mcp__deterministic__read_marker", {"marker": "ok"})
            self.assertIn("load_skill", result.tool_summary["allowedTools"])
            self.assertIn("mcp__deterministic__read_marker", result.tool_summary["allowedTools"])
            skill_results = [event for event in result.transcript_events if event.get("type") == "tool_result" and event.get("toolName") == "load_skill"]
            self.assertEqual("success", skill_results[0]["data"]["status"])
            self.assertEqual("sample-skill", skill_results[0]["data"]["name"])

    def test_native_tool_runner_read_only_denies_write_and_non_read_mcp_before_dispatch(self):
        import os
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as td:
            marker = os.path.join(td, "blocked.txt")
            client = StubToolClient([
                StubToolResponse("<summary>try writes</summary>", [
                    StubToolCall("file_write", {"path": marker, "content": "blocked"}, id="tool_write"),
                    StubToolCall("mcp__deterministic__write_marker", {"marker": "blocked"}, id="tool_mcp_write"),
                ]),
                StubToolResponse("<summary>done</summary>denied safely"),
            ])
            tools = [
                {"type": "function", "function": {"name": "file_write", "parameters": {"type": "object", "properties": {}}}},
                {"type": "function", "function": {"name": "mcp__deterministic__write_marker", "parameters": {"type": "object", "properties": {}}}},
            ]
            job = WorkflowJob(
                job_id="agent_read_only",
                prompt="try blocked writes",
                metadata={"runId": "wf_test", "permissionProfile": "read_only", "permissionPolicyVersion": "read-only-v1"},
            )
            runner = NativeGPTChildAgentRunner(client_factory=lambda config_name: client, tools_schema_factory=lambda: tools)

            with mock.patch("mcp_runtime.call_mcp_tool") as call_mcp:
                runner.start(job)
                result = self.wait_for_result(runner, job)

            self.assertEqual("succeeded", result.status)
            self.assertFalse(os.path.exists(marker))
            call_mcp.assert_not_called()
            self.assertIn("file_write", result.tool_summary["deniedTools"])
            self.assertIn("mcp__deterministic__write_marker", result.tool_summary["deniedTools"])
            self.assertEqual(2, result.tool_summary["denied"])
            self.assertTrue(any(event.get("type") == "tool_denied" and event.get("toolName") == "file_write" for event in result.transcript_events))
            self.assertTrue(any(event.get("type") == "tool_result" and event.get("data", {}).get("permission", {}).get("action") == "deny" for event in result.transcript_events))


if __name__ == "__main__":
    unittest.main()
