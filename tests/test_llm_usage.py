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
                "cached_tokens": 0,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
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
                "cached_tokens": 0,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
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
                "cached_tokens": 1,
                "cache_read_tokens": 1,
                "cache_creation_tokens": 2,
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
            {
                "input_tokens": 10,
                "output_tokens": 20,
                "total_tokens": 30,
                "cached_tokens": 0,
                "cache_read_tokens": 0,
                "cache_creation_tokens": 0,
            },
            session.last_usage_tokens,
        )

    def test_normalize_openai_usage_exposes_cached_input_tokens(self):
        self.assertEqual(
            {
                "input_tokens": 100,
                "output_tokens": 20,
                "total_tokens": 120,
                "cached_tokens": 70,
                "cache_read_tokens": 70,
                "cache_creation_tokens": 0,
            },
            normalize_usage_tokens(
                {
                    "input_tokens": 100,
                    "output_tokens": 20,
                    "total_tokens": 120,
                    "input_tokens_details": {"cached_tokens": 70},
                },
                "responses",
            ),
        )

    def test_normalize_chat_usage_exposes_cached_prompt_tokens(self):
        normalized = normalize_usage_tokens(
            {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
                "prompt_tokens_details": {"cached_tokens": 70},
            },
            "chat_completions",
        )
        self.assertEqual(70, normalized["cached_tokens"])
        self.assertEqual(70, normalized["cache_read_tokens"])


if __name__ == "__main__":
    unittest.main()
