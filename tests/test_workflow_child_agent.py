import json
import tempfile
import threading
import time
import unittest
from unittest import mock
from pathlib import Path

from workflow_child_agent import NativeGPTChildAgentRunner, bounded_structured_summary
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
    def test_child_cwd_uses_workspace_path_from_job_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            workspace = Path(tmp) / "workspace"
            workspace.mkdir()
            job = WorkflowJob(
                job_id="agent_1",
                prompt="write a relative file",
                metadata={"runId": "wf_test", "workspacePath": str(workspace)},
            )
            runner = NativeGPTChildAgentRunner()

            self.assertEqual(str(workspace.resolve()), runner._child_cwd(job))

    def test_child_agent_system_prompt_includes_optional_skill_listing(self):
        runner = NativeGPTChildAgentRunner(system_prompt="base prompt")

        with mock.patch("ga_agents_runtime.build_ga_project_instructions", return_value="\n[GA_PROJECT_INSTRUCTIONS]\nproject fake\n[/GA_PROJECT_INSTRUCTIONS]\n"):
            with mock.patch("skills_runtime.build_skill_prompt", return_value="\n[Available Skills]\nWhen matched, call load_skill.\n- using-superpowers\n"):
                prompt = runner._build_system_prompt()

        self.assertIn("base prompt", prompt)
        self.assertIn("[GA_PROJECT_INSTRUCTIONS]", prompt)
        self.assertIn("project fake", prompt)
        self.assertIn("[Available Skills]", prompt)
        self.assertLess(prompt.index("[GA_PROJECT_INSTRUCTIONS]"), prompt.index("[Available Skills]"))
        self.assertIn("call load_skill", prompt)
        self.assertIn("using-superpowers", prompt)

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
            # factory key is profile/config label; no longer hard-coded mykey native_oai_config
            self.assertTrue(isinstance(config_name, str))
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
            profile_name="test-profile",
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

    def test_child_prompt_uses_bounded_dependency_handoff_instead_of_transcript(self):
        job = WorkflowJob(
            job_id="agent_2",
            prompt="write the final report",
            metadata={
                "runId": "wf_test",
                "label": "Writer",
                "dependsOn": ["Researcher"],
                "dependencyHandoff": [
                    {
                        "label": "Researcher",
                        "status": "succeeded",
                        "summary": "short research conclusion",
                        "resultRef": "agents/agent_1/result.json",
                        "artifactRefs": ["artifacts/research.json"],
                        "transcriptRef": "agents/agent_1/transcript.jsonl",
                    }
                ],
            },
        )
        runner = NativeGPTChildAgentRunner()

        prompt = runner._build_prompt(job)

        self.assertIn("Dependency handoff", prompt)
        self.assertIn("short research conclusion", prompt)
        self.assertIn("artifacts/research.json", prompt)
        self.assertIn("resultRef: agents/agent_1/result.json", prompt)
        self.assertNotIn("x" * 1000, prompt)

    def test_child_prompt_explains_which_root_each_dependency_ref_uses(self):
        """Regression: the child read ``resultRef`` as workspace-relative.

        Run ``wf_d1790ea5023946e082b973d9d550de39``: the synthesis child looked
        for ``agents/agent_1/result.json`` inside its own workspace, did not find
        it (the file lives in the run's internal artifact directory), and reported
        the upstream sources as unrecoverable. The prompt must say which root each
        ref resolves under, and point at the readable host copy.
        """
        job = WorkflowJob(
            job_id="agent_2",
            prompt="write the final report",
            metadata={
                "runId": "wf_test",
                "label": "Writer",
                "dependsOn": ["Researcher"],
                "dependencyHandoff": [
                    {
                        "label": "Researcher",
                        "status": "succeeded",
                        "summary": "short research conclusion",
                        "resultRef": "agents/agent_1/result.json",
                        "upstreamResultPath": "workflow-handoffs/upstream-agent_1.json",
                        "artifactRefs": ["artifacts/research.json"],
                        "transcriptRef": "agents/agent_1/transcript.jsonl",
                    }
                ],
            },
        )
        runner = NativeGPTChildAgentRunner()

        prompt = runner._build_prompt(job)

        self.assertIn("upstreamResultPath", prompt)
        self.assertIn("workflow-handoffs/upstream-agent_1.json", prompt)
        self.assertIn("Path bases:", prompt)
        self.assertIn("do not try to open them", prompt)

    def test_bounded_summary_keeps_a_long_structured_answer_parseable(self):
        """Regression: a 2000-character cut left the handoff as half a JSON object.

        The synthesis child then read ``sources[0]`` as if it were the whole
        upstream source list. A structured summary must stay valid JSON and state
        where the full value lives.
        """
        answer = json.dumps(
            {
                "sources": [{"id": f"S{i}", "url": f"https://example.com/{i}", "notes": "n" * 120} for i in range(20)],
                "claims": [{"id": f"C{i}", "text": "t" * 120} for i in range(20)],
            },
            ensure_ascii=False,
            indent=2,
        )

        summary = bounded_structured_summary(answer, ref="agents/agent_1/result.json")

        self.assertLessEqual(len(summary), 2_000)
        parsed = json.loads(summary)
        self.assertTrue(parsed["_truncated"])
        self.assertIn("agents/agent_1/result.json", parsed["_note"])
        self.assertTrue(parsed["sources"])

    def test_bounded_summary_marks_a_truncated_prose_answer(self):
        text = chr(10).join(f"line {index} of a long prose answer" for index in range(400))

        summary = bounded_structured_summary(text)

        self.assertLessEqual(len(summary), 2_000)
        self.assertIn("truncated by the host", summary)
        self.assertIn("line 0 of a long prose answer", summary)
    def test_bounded_summary_flags_an_already_truncated_fragment(self):
        """A fragment left by an earlier fixed cut must not pass as complete data.

        Run ``wf_d1790ea5...`` stored exactly this shape: 2000 characters of JSON
        that stop inside ``sources[0]``. Bounded again at handoff time it stayed a
        silent 516-character fragment.
        """
        fragment = '{"sources": [{"id": "S1", "url": "https://example.com/1", "notes": "unfinished'

        summary = bounded_structured_summary(fragment, ref="agents/agent_1/result.json")

        self.assertIn("truncated by the host", summary)
        self.assertIn("agents/agent_1/result.json", summary)

    def test_child_prompt_says_which_job_wrote_each_upstream_artifact(self):
        """The dependent child must know whose output each ref is.

        Regression: the child saw ``artifactRefs`` only, so it could not tell a
        research artifact from another stage's overwrite of the same path.
        """
        job = WorkflowJob(
            job_id="agent_2",
            prompt="write the final report",
            metadata={
                "runId": "wf_test",
                "label": "Writer",
                "dependsOn": ["Researcher"],
                "dependencyHandoff": [
                    {
                        "label": "Researcher",
                        "status": "succeeded",
                        "summary": "short research conclusion",
                        "resultRef": "agents/agent_1/result.json",
                        "artifactRefs": ["artifacts/research.json"],
                        "artifactOwners": {"artifacts/research.json": ["Researcher"]},
                        "transcriptRef": "agents/agent_1/transcript.jsonl",
                    }
                ],
            },
        )
        runner = NativeGPTChildAgentRunner()

        prompt = runner._build_prompt(job)

        self.assertIn("artifactOwners (which job wrote each ref)", prompt)
        self.assertIn("artifacts/research.json <- Researcher", prompt)

    def test_child_prompt_declares_structured_output_contract_for_schema_jobs(self):
        """A schema job must tell the child that its answer is machine-validated JSON.

        Regression: the child prompt only embedded ``options`` as an opaque dict,
        so the model answered in prose and the host schema check could never pass.
        Step-Code appends an explicit ``<workflow-structured-output>`` block; GA
        must carry the same contract.
        """
        job = WorkflowJob(
            job_id="agent_1",
            prompt="collect sources",
            metadata={
                "runId": "wf_test",
                "label": "source-discovery",
                "options": {
                    "schema": {
                        "type": "object",
                        "required": ["sources", "claims", "risks"],
                        "properties": {"sources": {"type": "array"}},
                    }
                },
            },
        )
        runner = NativeGPTChildAgentRunner()

        prompt = runner._build_prompt(job)

        self.assertIn("<workflow-structured-output>", prompt)
        self.assertIn("</workflow-structured-output>", prompt)
        self.assertIn("Return exactly one JSON value", prompt)
        self.assertIn('"sources"', prompt)
        self.assertIn('"claims"', prompt)
        self.assertIn('"risks"', prompt)
        self.assertIn("Do not wrap it in Markdown", prompt)

    def test_child_prompt_omits_structured_output_contract_without_schema(self):
        job = WorkflowJob(job_id="agent_1", prompt="just summarize", metadata={"runId": "wf_test"})
        runner = NativeGPTChildAgentRunner()

        prompt = runner._build_prompt(job)

        self.assertNotIn("<workflow-structured-output>", prompt)

    def test_child_prompt_carries_previous_schema_failure_feedback_on_retry(self):
        """A retry that repeats the identical prompt just fails the same way."""
        job = WorkflowJob(
            job_id="agent_1",
            prompt="collect sources",
            metadata={
                "runId": "wf_test",
                "options": {"schema": {"type": "object", "required": ["sources"]}},
                "retryFeedback": {
                    "attempt": 2,
                    "issues": ["missing required field: sources", "missing required field: claims"],
                },
            },
        )
        runner = NativeGPTChildAgentRunner()

        prompt = runner._build_prompt(job)

        self.assertIn("<workflow-retry", prompt)
        self.assertIn("missing required field: sources", prompt)
        self.assertIn("missing required field: claims", prompt)

    def test_native_runner_parses_schema_fenced_json_answer_into_payload(self):
        """The host validates ``payload``; a JSON answer must land there, not in prose."""
        answer = "\n".join(
            [
                "<summary>collected sources</summary>",
                "Here is the structured result:",
                "```json",
                "{",
                '  "sources": [{"id": "S1", "url": "https://example.com/a"}],',
                '  "claims": [{"id": "C1", "text": "landed"}],',
                '  "risks": [{"id": "R1", "text": "unverified"}]',
                "}",
                "```",
            ]
        )
        job = WorkflowJob(
            job_id="agent_1",
            prompt="collect sources",
            metadata={
                "runId": "wf_test",
                "options": {"schema": {"type": "object", "required": ["sources", "claims", "risks"]}},
            },
        )
        runner = NativeGPTChildAgentRunner(session_factory=lambda config_name: StubSession(chunks=(answer,)))

        runner.start(job)
        result = self.wait_for_result(runner, job)

        self.assertEqual("succeeded", result.status)
        self.assertIsInstance(result.payload, dict)
        self.assertEqual([{"id": "S1", "url": "https://example.com/a"}], result.payload["sources"])
        self.assertEqual([{"id": "C1", "text": "landed"}], result.payload["claims"])
        self.assertEqual([{"id": "R1", "text": "unverified"}], result.payload["risks"])
        assistant_events = [event for event in result.transcript_events if event.get("type") == "assistant"]
        self.assertEqual(answer, assistant_events[0]["text"])

    def test_native_runner_keeps_prose_payload_when_answer_is_not_json(self):
        job = WorkflowJob(
            job_id="agent_1",
            prompt="collect sources",
            metadata={"runId": "wf_test", "options": {"schema": {"type": "object", "required": ["sources"]}}},
        )
        runner = NativeGPTChildAgentRunner(
            session_factory=lambda config_name: StubSession(chunks=("plain prose, no json here",))
        )

        runner.start(job)
        result = self.wait_for_result(runner, job)

        self.assertEqual("succeeded", result.status)
        self.assertEqual("plain prose, no json here", result.payload["summary"])
        self.assertEqual("plain prose, no json here", result.payload["text"])

    def test_native_runner_preserves_long_structured_json_answer(self):
        """Display compaction must never truncate a machine-validated answer.

        Regression: ``agent_runner_loop`` shrank any fenced block longer than six
        lines to ``... (N lines)``, which silently destroyed a valid JSON answer.
        """
        answer = "```json\n" + json.dumps(
            {
                "sources": [{"id": f"S{index}", "url": f"https://example.com/{index}"} for index in range(12)],
                "claims": [{"id": "C1", "text": "x" * 200}],
                "risks": ["unverified"],
            },
            ensure_ascii=False,
            indent=2,
        ) + "\n```"
        job = WorkflowJob(
            job_id="agent_1",
            prompt="collect sources",
            metadata={
                "runId": "wf_test",
                "options": {"schema": {"type": "object", "required": ["sources", "claims", "risks"]}},
            },
        )
        runner = NativeGPTChildAgentRunner(session_factory=lambda config_name: StubSession(chunks=(answer,)))

        runner.start(job)
        result = self.wait_for_result(runner, job)

        self.assertEqual("succeeded", result.status)
        self.assertEqual(12, len(result.payload["sources"]))
        assistant_events = [event for event in result.transcript_events if event.get("type") == "assistant"]
        self.assertNotIn("lines)", assistant_events[0]["text"])

    def test_child_prompt_and_transcript_carry_workflow_role(self):
        created = []

        def factory(config_name):
            session = StubSession()
            created.append(session)
            return session

        job = WorkflowJob(
            job_id="agent_1",
            prompt="run verification",
            phase="Verification",
            metadata={"runId": "wf_test", "label": "verify", "options": {"role": "verification"}},
        )
        runner = NativeGPTChildAgentRunner(session_factory=factory)

        runner.start(job)
        result = self.wait_for_result(runner, job)

        self.assertIn("role: verification", created[0].prompts[0])
        self.assertEqual("verification", result.transcript_events[0]["options"]["role"])

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

        with mock.patch("ga.GenericAgentHandler.do_file_read", return_value=StepOutcome({"status": "success", "content": "token=result-secret Cookie: sid=cookie_secret request_id=req_123"})), mock.patch("mcp_runtime.discover_mcp_tools_cached", return_value=[]):
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

    def test_cancel_stops_active_mcp_dispatch(self):
        tool_started = threading.Event()
        release_tool = threading.Event()
        client = StubToolClient([
            StubToolResponse("<summary>call mcp</summary>", [
                StubToolCall("mcp__deterministic__hang", {}, id="tool_mcp"),
            ]),
            StubToolResponse("<summary>done</summary>cancelled tool handled"),
        ])
        tools = [
            {"type": "function", "function": {"name": "mcp__deterministic__hang", "parameters": {"type": "object", "properties": {}}}},
        ]
        job = WorkflowJob(job_id="agent_cancel_mcp", prompt="call the blocking MCP tool", metadata={"runId": "wf_test"})
        runner = NativeGPTChildAgentRunner(client_factory=lambda config_name: client, tools_schema_factory=lambda: tools)

        def blocking_mcp_call(_name, _arguments):
            from mcp_runtime import _current_stop_signal

            tool_started.set()
            while not release_tool.is_set():
                stop_signal = _current_stop_signal()
                if stop_signal is not None and stop_signal.is_set():
                    return {"status": "error", "msg": "MCP call aborted by user"}
                time.sleep(0.01)
            return {"status": "error", "msg": "test cleanup released tool"}

        result = None
        with mock.patch("mcp_runtime.call_mcp_tool", side_effect=blocking_mcp_call), mock.patch("mcp_runtime.discover_mcp_tools_cached", return_value=[]):
            runner.start(job)
            self.assertTrue(tool_started.wait(timeout=2), "workflow child did not enter MCP dispatch")
            runner.cancel(job)
            deadline = time.monotonic() + 1
            while result is None and time.monotonic() < deadline:
                result = runner.poll(job)
                time.sleep(0.01)
            stopped_quickly = result is not None
            release_tool.set()
            if result is None:
                result = self.wait_for_result(runner, job, timeout=2)

        self.assertTrue(stopped_quickly, "workflow child MCP dispatch ignored runner.cancel()")
        self.assertTrue(client.cancelled)
        self.assertIsNotNone(result)
        self.assertEqual(len(client.requests), 1, "cancelled workflow child requested another model turn")
        self.assertEqual(result.status, "cancelled")

    def test_cancel_stops_mcp_discovery_before_first_model_request(self):
        discovery_started = threading.Event()
        release_discovery = threading.Event()
        client = StubToolClient([])
        job = WorkflowJob(job_id="agent_cancel_discovery", prompt="discover tools", metadata={"runId": "wf_test"})
        runner = NativeGPTChildAgentRunner(client_factory=lambda config_name: client)

        def blocking_discovery(*_args, **_kwargs):
            from mcp_runtime import _current_stop_signal

            discovery_started.set()
            while not release_discovery.is_set():
                stop_signal = _current_stop_signal()
                if stop_signal is not None and stop_signal.is_set():
                    return []
                time.sleep(0.01)
            return []

        result = None
        with mock.patch("mcp_runtime.discover_mcp_tools_cached", side_effect=blocking_discovery):
            runner.start(job)
            self.assertTrue(discovery_started.wait(timeout=2), "workflow child did not enter MCP discovery")
            runner.cancel(job)
            deadline = time.monotonic() + 0.75
            while result is None and time.monotonic() < deadline:
                result = runner.poll(job)
                time.sleep(0.01)
            stopped_quickly = result is not None
            release_discovery.set()
            if result is None:
                result = self.wait_for_result(runner, job, timeout=2)

        self.assertTrue(stopped_quickly, "workflow child cancellation did not interrupt MCP discovery")
        self.assertEqual(client.requests, [])
        self.assertEqual(result.status, "cancelled")

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

    def test_default_tool_schema_exposes_full_static_skill_and_discovered_mcp_tools(self):
        mcp_tool = {"type": "function", "function": {"name": "mcp__deterministic__read_marker", "parameters": {"type": "object", "properties": {}}}}
        runner = NativeGPTChildAgentRunner(enable_tools=True)

        with mock.patch("mcp_runtime.discover_mcp_tools_cached", return_value=[mcp_tool]):
            tools = runner._load_tools_schema()

        tool_names = {(tool.get("function") or {}).get("name") for tool in tools}
        self.assertIn("file_read", tool_names)
        self.assertIn("file_write", tool_names)
        self.assertIn("load_skill", tool_names)
        self.assertIn("mcp__deterministic__read_marker", tool_names)
        self.assertGreater(len(tool_names), 3, "default workflow child tools must not be file-only/minimal")

    def test_default_tool_schema_deduplicates_discovered_mcp_tools(self):
        duplicate = {"type": "function", "function": {"name": "file_read", "parameters": {"type": "object", "properties": {}}}}
        runner = NativeGPTChildAgentRunner(enable_tools=True)

        with mock.patch("mcp_runtime.discover_mcp_tools_cached", return_value=[duplicate]):
            tools = runner._load_tools_schema()

        tool_names = [(tool.get("function") or {}).get("name") for tool in tools]
        self.assertEqual(1, tool_names.count("file_read"))

    def test_tools_schema_factory_transforms_default_and_discovered_tools_instead_of_replacing_them(self):
        seen_by_factory = []
        mcp_tool = {"type": "function", "function": {"name": "mcp__deterministic__read_marker", "parameters": {"type": "object", "properties": {}}}}

        def factory(tools):
            seen_by_factory.extend((tool.get("function") or {}).get("name") for tool in tools)
            return list(tools)

        runner = NativeGPTChildAgentRunner(enable_tools=True, tools_schema_factory=factory)

        with mock.patch("mcp_runtime.discover_mcp_tools_cached", return_value=[mcp_tool]):
            tools = runner._load_tools_schema()

        tool_names = {(tool.get("function") or {}).get("name") for tool in tools}
        self.assertIn("file_read", tool_names)
        self.assertIn("load_skill", tool_names)
        self.assertIn("mcp__deterministic__read_marker", tool_names)
        self.assertIn("file_read", seen_by_factory)
        self.assertIn("load_skill", seen_by_factory)
        self.assertIn("mcp__deterministic__read_marker", seen_by_factory)

    def test_zero_arg_tools_schema_factory_keeps_required_skill_and_mcp_capabilities(self):
        selected_only_tool = {"type": "function", "function": {"name": "custom_selected_tool", "parameters": {"type": "object", "properties": {}}}}
        mcp_tool = {"type": "function", "function": {"name": "mcp__deterministic__read_marker", "parameters": {"type": "object", "properties": {}}}}
        runner = NativeGPTChildAgentRunner(enable_tools=True, tools_schema_factory=lambda: [selected_only_tool])

        with mock.patch("mcp_runtime.discover_mcp_tools_cached", return_value=[mcp_tool]):
            tools = runner._load_tools_schema()

        tool_names = {(tool.get("function") or {}).get("name") for tool in tools}
        self.assertIn("custom_selected_tool", tool_names)
        self.assertIn("file_read", tool_names)
        self.assertIn("load_skill", tool_names)
        self.assertIn("mcp__deterministic__read_marker", tool_names)

    def test_mcp_discovery_failure_records_warning_without_dropping_base_tools(self):
        runner = NativeGPTChildAgentRunner(enable_tools=True)

        with mock.patch("mcp_runtime.discover_mcp_tools_cached", side_effect=RuntimeError("boom token=secret")):
            tools = runner._load_tools_schema()

        tool_names = {(tool.get("function") or {}).get("name") for tool in tools}
        self.assertIn("file_read", tool_names)
        self.assertIn("load_skill", tool_names)
        snapshot = runner.last_capability_snapshot
        self.assertEqual("error", snapshot["mcpDiscovery"]["status"])
        self.assertEqual(0, snapshot["mcpDiscovery"]["injectedToolCount"])
        self.assertEqual("RuntimeError", snapshot["mcpDiscovery"]["errorType"])
        self.assertNotIn("secret", snapshot["mcpDiscovery"].get("error", ""))

    def test_child_agent_result_includes_capability_snapshot_for_advertised_tools(self):
        mcp_tool = {"type": "function", "function": {"name": "mcp__deterministic__read_marker", "parameters": {"type": "object", "properties": {}}}}
        client = StubToolClient([StubToolResponse("<summary>done</summary>capabilities observed")])
        job = WorkflowJob(job_id="agent_caps", prompt="finish", metadata={"runId": "wf_test"})
        runner = NativeGPTChildAgentRunner(client_factory=lambda config_name: client)

        with mock.patch("mcp_runtime.discover_mcp_tools_cached", return_value=[mcp_tool]):
            runner.start(job)
            result = self.wait_for_result(runner, job)

        snapshots = [event for event in result.transcript_events if event.get("type") == "capability_snapshot"]
        self.assertEqual(1, len(snapshots))
        capabilities = snapshots[0]["capabilities"]
        self.assertTrue(capabilities["loadSkillAvailable"])
        self.assertTrue(capabilities["fileReadAvailable"])
        self.assertIn("mcp__deterministic__read_marker", capabilities["mcpToolNames"])
        self.assertEqual("ok", capabilities["mcpDiscovery"]["status"])

    def test_native_tool_runner_allows_file_read_through_generic_handler_dispatch(self):
        client = StubToolClient([
            StubToolResponse("<summary>need read</summary>", [StubToolCall("file_read", {"path": __file__, "show_linenos": False})]),
            StubToolResponse("<summary>done</summary>read complete"),
        ])
        job = WorkflowJob(
            job_id="agent_tool_read",
            prompt="read test file",
            metadata={"runId": "wf_test", "permissionProfile": "inherit-current-permissions", "permissionPolicyVersion": "inherit-current-v1", "workspacePath": str(Path(__file__).resolve().parent.parent)},
        )
        tools = [
            {"type": "function", "function": {"name": "file_read", "parameters": {"type": "object", "properties": {}}}},
        ]
        runner = NativeGPTChildAgentRunner(client_factory=lambda config_name: client, tools_schema_factory=lambda: tools)

        with mock.patch("mcp_runtime.discover_mcp_tools_cached", return_value=[]):
            runner.start(job)
            result = self.wait_for_result(runner, job)

        self.assertEqual("succeeded", result.status)
        self.assertIn("read complete", result.payload["text"])
        self.assertEqual({"input_tokens": 5, "output_tokens": 4}, result.token_usage)
        self.assertTrue(client.tools_seen)
        advertised = {(tool.get("function") or {}).get("name") for tool in client.tools_seen[0]}
        self.assertIn("file_read", advertised)
        self.assertIn("load_skill", advertised)
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

            with mock.patch("mcp_runtime.call_mcp_tool", return_value={"status": "success", "marker": "ok"}) as call_mcp, mock.patch("mcp_runtime.discover_mcp_tools_cached", return_value=[]):
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

            with mock.patch("mcp_runtime.call_mcp_tool") as call_mcp, mock.patch("mcp_runtime.discover_mcp_tools_cached", return_value=[]):
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

    def test_child_tool_summary_reports_files_written_by_code_run(self):
        """The runner must report artifacts from the filesystem, not tool names.

        ``observedArtifacts`` used to be rebuilt downstream by scanning
        transcript events for ``file_write``/``file_patch``, so a file created by
        ``code_run`` -- the normal path for DOCX/HTML generation -- was never
        observed and never reached the handoff. The runner now diffs the
        workspace, which is tool-agnostic by construction.
        """
        import os
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td) / "workspace"
            workspace.mkdir()
            script = (
                "from pathlib import Path\n"
                "Path('report.json').write_text('{\"ok\": true}', encoding='utf-8')\n"
                "print('written')\n"
            )
            client = StubToolClient([
                StubToolResponse("<summary>write via code_run</summary>", [
                    StubToolCall("code_run", {"type": "python", "code": script}, id="tool_code"),
                ]),
                StubToolResponse("<summary>done</summary>code_run complete"),
            ])
            tools = [
                {"type": "function", "function": {"name": "code_run", "parameters": {"type": "object", "properties": {}}}},
            ]
            job = WorkflowJob(
                job_id="agent_code_run_writer",
                prompt="write a report with code_run",
                metadata={
                    "runId": "wf_test",
                    "permissionProfile": "inherit-current-permissions",
                    "permissionPolicyVersion": "inherit-current-v1",
                    "workspacePath": str(workspace),
                },
            )
            runner = NativeGPTChildAgentRunner(client_factory=lambda config_name: client, tools_schema_factory=lambda: tools)

            with mock.patch("mcp_runtime.discover_mcp_tools_cached", return_value=[]):
                runner.start(job)
                result = self.wait_for_result(runner, job)

            self.assertEqual("succeeded", result.status)
            self.assertTrue((workspace / "report.json").is_file(), "code_run should have created the file")
            self.assertIn("report.json", result.tool_summary.get("writtenPaths") or [])
            self.assertNotIn("file_write", result.tool_summary.get("allowedTools") or [])

    def test_child_tool_summary_detects_a_write_from_a_non_writer_tool_name(self):
        """Observation must key off the filesystem, not a tool-name allowlist.

        The recorded tool name here is ``file_read``, which the host does not
        classify as a writer. The workspace diff still reports the new file,
        which is exactly the shape a future writer tool has before anyone
        teaches the host about it.
        """
        import tempfile
        from unittest import mock

        with tempfile.TemporaryDirectory() as td:
            workspace = Path(td) / "workspace"
            workspace.mkdir()
            (workspace / "seed.txt").write_text("seed", encoding="utf-8")

            client = StubToolClient([
                StubToolResponse("<summary>read and produce</summary>", [
                    StubToolCall("file_read", {"path": "seed.txt", "show_linenos": False}, id="tool_read"),
                ]),
                StubToolResponse("<summary>done</summary>non-writer tool complete"),
            ])
            job = WorkflowJob(
                job_id="agent_future_writer",
                prompt="produce a file through a tool the host calls read-only",
                metadata={
                    "runId": "wf_test",
                    "permissionProfile": "inherit-current-permissions",
                    "permissionPolicyVersion": "inherit-current-v1",
                    "workspacePath": str(workspace),
                },
            )
            runner = NativeGPTChildAgentRunner(client_factory=lambda config_name: client)
            original_build_handler = runner._build_handler

            def build_handler_with_future_tool(job_arg, transcript_events, profile, version):
                handler = original_build_handler(job_arg, transcript_events, profile, version)
                inner = handler.tool_before_callback

                def before(tool_name, args, response):
                    if tool_name == "file_read":
                        (workspace / "from_future_tool.txt").write_text("payload", encoding="utf-8")
                    return inner(tool_name, args, response)

                handler.tool_before_callback = before
                return handler

            with mock.patch("mcp_runtime.discover_mcp_tools_cached", return_value=[]), \
                 mock.patch.object(runner, "_build_handler", side_effect=build_handler_with_future_tool):
                runner.start(job)
                result = self.wait_for_result(runner, job)

            self.assertEqual("succeeded", result.status)
            self.assertIn("from_future_tool.txt", result.tool_summary.get("writtenPaths") or [])
            self.assertIn("file_read", {event.get("toolName") for event in result.transcript_events if event.get("type") == "tool_call"})
            self.assertNotIn("file_write", {event.get("toolName") for event in result.transcript_events if event.get("type") == "tool_call"})


if __name__ == "__main__":
    unittest.main()
