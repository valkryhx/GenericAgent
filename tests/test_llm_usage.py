import unittest

import json

from llmcore import _parse_claude_sse, normalize_usage_tokens


class LLMUsageTest(unittest.TestCase):
    def test_normalize_openai_chat_usage(self):
        self.assertEqual(
            {
                "input_tokens": 11,
                "output_tokens": 17,
                "total_tokens": 28,
            },
            normalize_usage_tokens(
                {
                    "prompt_tokens": 11,
                    "completion_tokens": 17,
                    "total_tokens": 28,
                    "prompt_tokens_details": {"cached_tokens": 0},
                },
                "chat_completions",
            ),
        )

    def test_normalize_responses_usage(self):
        self.assertEqual(
            {
                "input_tokens": 5,
                "output_tokens": 7,
                "total_tokens": 12,
            },
            normalize_usage_tokens(
                {
                    "input_tokens": 5,
                    "output_tokens": 7,
                    "total_tokens": 12,
                },
                "responses",
            ),
        )

    def test_normalize_anthropic_messages_usage_includes_cache_tokens_in_total(self):
        self.assertEqual(
            {
                "input_tokens": 10,
                "output_tokens": 20,
                "total_tokens": 33,
            },
            normalize_usage_tokens(
                {
                    "input_tokens": 10,
                    "output_tokens": 20,
                    "cache_creation_input_tokens": 2,
                    "cache_read_input_tokens": 1,
                },
                "messages",
            ),
        )

    def test_claude_stream_usage_merges_input_and_output_events(self):
        class Session:
            last_usage_tokens = None

        lines = [
            "data: " + json.dumps({"type": "message_start", "message": {"usage": {"input_tokens": 10}}}),
            "data: " + json.dumps({"type": "message_delta", "delta": {"stop_reason": "end_turn"}, "usage": {"output_tokens": 20}}),
            "data: " + json.dumps({"type": "message_stop"}),
        ]

        session = Session()
        list(_parse_claude_sse(lines, session))

        self.assertEqual(
            {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
            session.last_usage_tokens,
        )


if __name__ == "__main__":
    unittest.main()
