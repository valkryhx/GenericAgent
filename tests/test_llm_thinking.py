"""实时 thinking：解析层必须把推理块交给前端，且不能混进正文流。

回归背景：`_parse_claude_sse` 的 `thinking_delta` 只累加 `current_block`，
`_parse_openai_sse` 的 `reasoning_text` 同样只进 blocks。两份数据一直存在
（`MockResponse.thinking` 有值），但 `chat()` 的 yield 契约是「显示文本」，
所以推理从来没有离开过后端 —— 用户既看不到 resume 的思考，也看不到实时的。

修法是块完成时通过 `on_thinking` 回调发出（`_notify_thinking`），而不是塞进
yield 流。这三组测试分别钉住：回调收到、正文流没被污染、块顺序正确。
"""

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from genericagent.llm.core import (  # noqa: E402
    _parse_claude_sse,
    _parse_openai_json,
    _parse_openai_sse,
)


def sse(events):
    return [("data: " + json.dumps(event)).encode("utf-8") for event in events]


class ThinkingSink:
    """Minimal session stub: records on_thinking payloads, accepts usage bookkeeping."""

    def __init__(self):
        self.blocks = []
        self.on_retry_status = None
        self.on_thinking = self.blocks.append
        self.last_usage_tokens = None
        self.session_output_tokens = 0
        self.max_retries = 0
        self.stream = True


def drain(gen):
    """Run a parse generator to completion, returning (text, blocks)."""
    text = ""
    try:
        while True:
            text += next(gen)
    except StopIteration as stop:
        return text, (stop.value or [])


def thinking_texts(sink):
    return [payload.get("text") for payload in sink.blocks]


class ClaudeThinkingTest(unittest.TestCase):
    def test_thinking_block_reaches_the_callback(self):
        sink = ThinkingSink()
        events = [
            {"type": "message_start", "message": {"usage": {}}},
            {"type": "content_block_start", "content_block": {"type": "thinking"}},
            {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "REASON"}},
            {"type": "content_block_stop"},
            {"type": "content_block_start", "content_block": {"type": "text"}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "BODY"}},
            {"type": "content_block_stop"},
            {"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {}},
            {"type": "message_stop"},
        ]
        text, blocks = drain(_parse_claude_sse(iter(sse(events)), sink))

        self.assertEqual(thinking_texts(sink), ["REASON"])
        # Reasoning must not enter the display stream.
        self.assertEqual(text, "BODY")
        self.assertEqual([b["type"] for b in blocks], ["thinking", "text"])

    def test_callback_fires_before_any_prose_is_yielded(self):
        """The UI paints reasoning above prose, so it has to arrive first."""
        sink = ThinkingSink()
        order = []

        def record(payload):
            order.append(("thinking", payload["text"]))
            sink.blocks.append(payload)

        sink.on_thinking = record
        events = [
            {"type": "content_block_start", "content_block": {"type": "thinking"}},
            {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "R"}},
            {"type": "content_block_stop"},
            {"type": "content_block_start", "content_block": {"type": "text"}},
            {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "B"}},
            {"type": "content_block_stop"},
            {"type": "message_stop"},
        ]
        gen = _parse_claude_sse(iter(sse(events)), sink)
        try:
            while True:
                chunk = next(gen)
                if chunk:
                    order.append(("text", chunk))
        except StopIteration:
            pass

        self.assertEqual(order, [("thinking", "R"), ("text", "B")])

    def test_interrupted_stream_still_reports_partial_reasoning(self):
        """No content_block_stop: the reasoning that arrived is still worth showing."""
        sink = ThinkingSink()
        events = [
            {"type": "content_block_start", "content_block": {"type": "thinking"}},
            {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "PART"}},
        ]
        drain(_parse_claude_sse(iter(sse(events)), sink))
        self.assertEqual(thinking_texts(sink), ["PART"])

    def test_no_callback_configured_is_not_an_error(self):
        sink = ThinkingSink()
        sink.on_thinking = None
        events = [
            {"type": "content_block_start", "content_block": {"type": "thinking"}},
            {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "R"}},
            {"type": "content_block_stop"},
            {"type": "message_stop"},
        ]
        text, blocks = drain(_parse_claude_sse(iter(sse(events)), sink))
        self.assertEqual(text, "")
        self.assertEqual([b["type"] for b in blocks], ["thinking"])

    def test_blank_reasoning_emits_nothing(self):
        sink = ThinkingSink()
        events = [
            {"type": "content_block_start", "content_block": {"type": "thinking"}},
            {"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "   "}},
            {"type": "content_block_stop"},
            {"type": "message_stop"},
        ]
        drain(_parse_claude_sse(iter(sse(events)), sink))
        self.assertEqual(thinking_texts(sink), [])


class ChatCompletionsThinkingTest(unittest.TestCase):
    def test_reasoning_reaches_the_callback_before_content(self):
        sink = ThinkingSink()
        order = []

        def record(payload):
            order.append(("thinking", payload["text"]))

        sink.on_thinking = record
        events = [
            {"choices": [{"delta": {"reasoning_content": "THINK"}}]},
            {"choices": [{"delta": {"content": "BODY"}}]},
            {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        ]
        gen = _parse_openai_sse(iter(sse(events)), "chat_completions", sink)
        text = ""
        try:
            while True:
                chunk = next(gen)
                text += chunk
                if chunk:
                    order.append(("text", chunk))
        except StopIteration as stop:
            blocks = stop.value

        self.assertEqual(order, [("thinking", "THINK"), ("text", "BODY")])
        self.assertEqual(text, "BODY")
        self.assertEqual([b["type"] for b in blocks], ["thinking", "text"])

    def test_tool_only_turn_still_reports_reasoning(self):
        """No content delta to trigger the pre-prose emit: flush at stream end."""
        sink = ThinkingSink()
        events = [
            {"choices": [{"delta": {"reasoning": "PLAN"}}]},
            {"choices": [{"delta": {"tool_calls": [
                {"index": 0, "id": "call-1", "function": {"name": "code_run", "arguments": '{"code":"1"}'}},
            ]}}]},
            {"choices": [{"delta": {}, "finish_reason": "tool_calls"}]},
        ]
        text, blocks = drain(_parse_openai_sse(iter(sse(events)), "chat_completions", sink))

        self.assertEqual(thinking_texts(sink), ["PLAN"])
        self.assertIn("thinking", [b["type"] for b in blocks])
        self.assertIn("tool_use", [b["type"] for b in blocks])

    def test_non_streaming_json_reports_reasoning(self):
        sink = ThinkingSink()
        data = {
            "choices": [{"message": {"reasoning_content": "R", "content": "B"}, "finish_reason": "stop"}],
            "usage": {},
        }
        text, blocks = drain(_parse_openai_json(data, "chat_completions", sink))
        self.assertEqual(thinking_texts(sink), ["R"])
        self.assertEqual(text, "B")
        self.assertEqual([b["type"] for b in blocks], ["thinking", "text"])


class ResponsesThinkingTest(unittest.TestCase):
    def test_reasoning_summary_reaches_the_callback(self):
        sink = ThinkingSink()
        events = [
            {"type": "response.reasoning_summary_text.delta", "delta": "SUM"},
            {"type": "response.output_text.delta", "delta": "BODY"},
            {"type": "response.completed", "response": {"usage": {}}},
        ]
        text, blocks = drain(_parse_openai_sse(iter(sse(events)), "responses", sink))

        self.assertEqual(thinking_texts(sink), ["SUM"])
        self.assertEqual(text, "BODY")
        self.assertEqual([b["type"] for b in blocks], ["thinking", "text"])

    def test_non_streaming_reasoning_item(self):
        sink = ThinkingSink()
        data = {
            "status": "completed",
            "output": [
                {"type": "reasoning", "summary": [{"type": "summary_text", "text": "SUM"}]},
                {"type": "message", "content": [{"type": "output_text", "text": "BODY"}]},
            ],
            "usage": {},
        }
        text, blocks = drain(_parse_openai_json(data, "responses", sink))
        self.assertEqual(thinking_texts(sink), ["SUM"])
        self.assertEqual(text, "BODY")
        self.assertEqual([b["type"] for b in blocks], ["thinking", "text"])


if __name__ == "__main__":
    unittest.main()
