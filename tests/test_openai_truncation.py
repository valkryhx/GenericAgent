"""OpenAI truncation must surface as a sentinel instead of a silent end_turn.

Background: `_parse_openai_sse` (chat_completions branch) and `_parse_openai_json`
never read `finish_reason`. A response cut off by max_tokens therefore looked
like a normal completion: `do_no_tool` saw no warning, and MockResponse reported
stop_reason=end_turn. The Anthropic path already warned via `stop_reason ==
"max_tokens"`, so the OpenAI wires were the silent ones.

These tests assert on the emitted chunks and on stop_reason, i.e. the exact
signals the upstream loop dispatches on.
"""
import json
import unittest

from genericagent.llm import core as llm_core


# do_no_tool keys off this substring (genericagent/core/handler.py).
TRUNC_MARKER = "max_tokens !!!]"


def sse(*events):
    return [("data: " + json.dumps(e)).encode("utf-8") for e in events]


class ChatCompletionsStreamTruncationTest(unittest.TestCase):
    def _parse(self, events):
        chunks = list(llm_core._parse_openai_sse(
            iter(sse(*events)), "chat_completions", sess=None))
        blocks = chunks  # generator returns blocks via StopIteration; use helper below
        return chunks

    def _run(self, events):
        gen = llm_core._parse_openai_sse(iter(sse(*events)), "chat_completions", sess=None)
        chunks, blocks = [], None
        try:
            while True:
                chunks.append(next(gen))
        except StopIteration as e:
            blocks = e.value or []
        return chunks, blocks

    def test_finish_reason_length_emits_truncation_sentinel(self):
        chunks, blocks = self._run([
            {"choices": [{"delta": {"content": "partial output"}, "finish_reason": None}]},
            {"choices": [{"delta": {}, "finish_reason": "length"}]},
        ])
        joined = "".join(chunks)
        self.assertIn("partial output", joined)
        self.assertIn(TRUNC_MARKER, joined)
        # the warning must also land in the returned blocks, because the history
        # and MockResponse.content are both built from blocks.
        self.assertTrue(any(TRUNC_MARKER in (b.get("text") or "") for b in blocks))

    def test_finish_reason_stop_emits_no_sentinel(self):
        chunks, blocks = self._run([
            {"choices": [{"delta": {"content": "done"}, "finish_reason": "stop"}]},
        ])
        joined = "".join(chunks)
        self.assertIn("done", joined)
        self.assertNotIn("Response truncated", joined)
        self.assertFalse(any("Response truncated" in (b.get("text") or "") for b in blocks))

    def test_finish_reason_content_filter_is_visible_but_not_the_retry_marker(self):
        # content_filter must be visible, but must NOT reuse the max_tokens wording:
        # do_no_tool would then retry the same payload, which cannot succeed.
        chunks, _ = self._run([
            {"choices": [{"delta": {}, "finish_reason": "content_filter"}]},
        ])
        joined = "".join(chunks)
        self.assertIn("content_filter", joined)
        self.assertNotIn(TRUNC_MARKER, joined)

    def test_later_null_finish_reason_does_not_erase_a_real_one(self):
        # Some gateways send finish_reason on an early frame and null afterwards.
        # Keep the real value instead of letting the trailing null win.
        chunks, _ = self._run([
            {"choices": [{"delta": {"content": "a"}, "finish_reason": "length"}]},
            {"choices": [{"delta": {}, "finish_reason": None}]},
        ])
        self.assertIn(TRUNC_MARKER, "".join(chunks))

    def test_tool_call_finish_does_not_warn(self):
        chunks, blocks = self._run([
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call_1",
                 "function": {"name": "read_file", "arguments": '{"p": 1}'}}]},
                "finish_reason": "tool_calls"}]},
        ])
        self.assertNotIn("Response truncated", "".join(chunks))
        self.assertTrue(any(b.get("type") == "tool_use" for b in blocks))


class ResponsesStreamTruncationTest(unittest.TestCase):
    def _run(self, events):
        gen = llm_core._parse_openai_sse(iter(sse(*events)), "responses", sess=None)
        chunks, blocks = [], None
        try:
            while True:
                chunks.append(next(gen))
        except StopIteration as e:
            blocks = e.value or []
        return chunks, blocks

    def test_incomplete_max_output_tokens_is_a_truncation_not_a_fatal_error(self):
        # Previously this became "!!!Error: max_output_tokens", which do_no_tool
        # treats as a non-retryable provider error, so the turn just ended.
        chunks, _ = self._run([
            {"type": "response.output_text.delta", "delta": "partial"},
            {"type": "response.incomplete",
             "response": {"incomplete_details": {"reason": "max_output_tokens"}}},
        ])
        joined = "".join(chunks)
        self.assertIn("partial", joined)
        self.assertIn(TRUNC_MARKER, joined)
        self.assertNotIn("!!!Error", joined)

    def test_incomplete_for_other_reasons_stays_an_error(self):
        chunks, _ = self._run([
            {"type": "response.incomplete",
             "response": {"incomplete_details": {"reason": "content_filter"}}},
        ])
        joined = "".join(chunks)
        self.assertIn("!!!Error", joined)
        self.assertNotIn(TRUNC_MARKER, joined)

    def test_response_completed_carrying_incomplete_status_is_detected(self):
        # Some gateways never emit a separate response.incomplete event and put
        # the truncation on the terminal response.completed object instead.
        # Without handling this, that variant silently ends the turn — the exact
        # chat_completions bug in a different shape.
        chunks, blocks = self._run([
            {"type": "response.output_text.delta", "delta": "partial"},
            {"type": "response.completed",
             "response": {"status": "incomplete",
                          "incomplete_details": {"reason": "max_output_tokens"}}},
        ])
        joined = "".join(chunks)
        self.assertIn("partial", joined)
        self.assertIn(TRUNC_MARKER, joined)
        self.assertTrue(any(TRUNC_MARKER in (b.get("text") or "") for b in blocks))

    def test_response_completed_normal_status_is_quiet(self):
        chunks, _ = self._run([
            {"type": "response.output_text.delta", "delta": "done"},
            {"type": "response.completed",
             "response": {"status": "completed", "usage": {"output_tokens": 5}}},
        ])
        joined = "".join(chunks)
        self.assertIn("done", joined)
        self.assertNotIn("Response truncated", joined)

    def test_truncation_sentinel_is_emitted_only_once(self):
        # The sentinel must be emitted from the single post-loop decision point.
        # If a path also yielded it inline, the corpus would carry it twice and
        # do_no_tool would see a doubled marker.
        chunks, _ = self._run([
            {"type": "response.incomplete",
             "response": {"incomplete_details": {"reason": "max_output_tokens"}}},
        ])
        self.assertEqual(1, "".join(chunks).count(TRUNC_MARKER))

    def test_response_failed_is_still_an_error(self):
        chunks, _ = self._run([
            {"type": "response.failed",
             "response": {"error": {"message": "upstream boom"}}},
        ])
        joined = "".join(chunks)
        self.assertIn("!!!Error", joined)
        self.assertIn("upstream boom", joined)


class NonStreamingTruncationTest(unittest.TestCase):
    def _run(self, payload, api_mode):
        gen = llm_core._parse_openai_json(payload, api_mode, sess=None)
        chunks, blocks = [], None
        try:
            while True:
                chunks.append(next(gen))
        except StopIteration as e:
            blocks = e.value or []
        return chunks, blocks

    def test_chat_completions_length(self):
        chunks, blocks = self._run({
            "choices": [{"message": {"content": "partial"}, "finish_reason": "length"}],
        }, "chat_completions")
        self.assertIn(TRUNC_MARKER, "".join(chunks))
        self.assertTrue(any(TRUNC_MARKER in (b.get("text") or "") for b in blocks))

    def test_chat_completions_normal_completion_is_quiet(self):
        chunks, _ = self._run({
            "choices": [{"message": {"content": "fine"}, "finish_reason": "stop"}],
        }, "chat_completions")
        self.assertNotIn("Response truncated", "".join(chunks))

    def test_responses_incomplete(self):
        chunks, _ = self._run({
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [{"type": "message", "content": [
                {"type": "output_text", "text": "partial"}]}],
        }, "responses")
        joined = "".join(chunks)
        self.assertIn("partial", joined)
        self.assertIn(TRUNC_MARKER, joined)

    def test_responses_status_incomplete_without_details_still_warns(self):
        # A gateway may report status=incomplete with no incomplete_details.
        # Treating that as a clean finish would hide a truncated answer, so it
        # is reported as truncation rather than staying silent.
        chunks, _ = self._run({
            "status": "incomplete",
            "output": [{"type": "message", "content": [
                {"type": "output_text", "text": "partial"}]}],
        }, "responses")
        joined = "".join(chunks)
        self.assertIn("partial", joined)
        self.assertIn(TRUNC_MARKER, joined)

    def test_responses_completed_status_is_quiet(self):
        chunks, _ = self._run({
            "status": "completed",
            "output": [{"type": "message", "content": [
                {"type": "output_text", "text": "fine"}]}],
        }, "responses")
        self.assertNotIn("Response truncated", "".join(chunks))


class ProviderErrorDetectionTest(unittest.TestCase):
    """`error: null` is the Responses API's *normal* value and must not read as an error.

    Verified against the live gateway: every /responses payload carries an
    `error` key that is null on success. Treating key-presence as an error made
    perfectly good responses come back as "!!!Error: provider returned an
    unspecified error", which also hid real status=incomplete truncation.
    """

    def test_null_error_is_not_an_error(self):
        self.assertIsNone(llm_core._provider_error_message({"error": None}))

    def test_missing_error_key_is_not_an_error(self):
        self.assertIsNone(llm_core._provider_error_message({"status": "completed"}))

    def test_real_error_object_still_reports(self):
        self.assertEqual(
            "Upstream access forbidden",
            llm_core._provider_error_message(
                {"error": {"message": "Upstream access forbidden"}}),
        )

    def test_error_string_still_reports(self):
        self.assertEqual(
            "boom", llm_core._provider_error_message({"error": "boom"}))

    def test_empty_error_object_keeps_the_defensive_message(self):
        # An error key with no message is still suspicious; keep surfacing it
        # rather than silently treating the payload as clean.
        self.assertEqual(
            "provider returned an unspecified error",
            llm_core._provider_error_message({"error": {}}),
        )

    def test_truncated_responses_payload_is_not_mistaken_for_an_error(self):
        # End-to-end shape of a real truncated payload: error:null + incomplete.
        chunks, blocks = [], None
        gen = llm_core._parse_openai_json({
            "error": None,
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [{"type": "message", "content": [
                {"type": "output_text", "text": "partial"}]}],
        }, "responses", sess=None)
        try:
            while True:
                chunks.append(next(gen))
        except StopIteration as e:
            blocks = e.value or []
        joined = "".join(chunks)
        self.assertNotIn("!!!Error", joined)
        self.assertIn("partial", joined)
        self.assertIn(TRUNC_MARKER, joined)


class StopReasonPropagationTest(unittest.TestCase):
    """ask() must report the truncation through MockResponse.stop_reason."""

    class _Session(llm_core.NativeOAISession):
        def __init__(self, events):  # noqa: D107 - test double
            self._events = events
            self.history = []
            self.lock = __import__("threading").Lock()
            self.auto_compact_enabled = False
            self.last_usage_tokens = None
            self.session_output_tokens = 0
            self.system = ""
            self.tools = None
            self.max_tokens = 16384
            self.temperature = 1
            self.stream = True
            self.reasoning_effort = None
            self.service_tier = None
            self.model = "test-model"
            self.api_mode = "chat_completions"

        def raw_ask(self, messages):
            gen = llm_core._parse_openai_sse(
                iter(sse(*self._events)), "chat_completions", sess=None)
            return (yield from gen)

    def _ask(self, events):
        sess = self._Session(events)
        gen = sess.ask({"role": "user", "content": "hi"})
        out = []
        resp = None
        try:
            while True:
                out.append(next(gen))
        except StopIteration as e:
            resp = e.value
        return "".join(out), resp

    def test_truncated_turn_reports_max_tokens(self):
        text, resp = self._ask([
            {"choices": [{"delta": {"content": "cut"}, "finish_reason": "length"}]},
        ])
        self.assertIn("cut", text)
        self.assertIsNotNone(resp)
        self.assertEqual("max_tokens", resp.stop_reason)
        self.assertIn(TRUNC_MARKER, resp.content)

    def test_normal_turn_still_reports_end_turn(self):
        text, resp = self._ask([
            {"choices": [{"delta": {"content": "ok"}, "finish_reason": "stop"}]},
        ])
        self.assertEqual("end_turn", resp.stop_reason)
        self.assertNotIn("Response truncated", resp.content)


if __name__ == "__main__":
    unittest.main()
