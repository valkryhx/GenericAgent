import unittest
from types import SimpleNamespace
from unittest import mock

import llmcore


class ReasoningEffortWireTests(unittest.TestCase):
    def make_session(self, api_mode, effort):
        return SimpleNamespace(
            model="test-model",
            api_mode=api_mode,
            temperature=1,
            api_key="test-key",
            api_base="https://example.test/v1",
            stream=True,
            system="",
            reasoning_effort=effort,
            max_tokens=128,
            tools=None,
            service_tier=None,
        )

    def capture_payload(self, api_mode, effort):
        captured = {}

        def fake_stream(_sess, _url, _headers, payload, _parse_fn):
            captured["payload"] = payload
            if False:
                yield ""
            return []

        with mock.patch.object(llmcore, "_stream_with_retry", fake_stream):
            list(llmcore._openai_stream(self.make_session(api_mode, effort), [{"role": "user", "content": "hi"}]))
        return captured["payload"]

    def test_responses_preserves_none_and_ultra(self):
        self.assertEqual(
            self.capture_payload("responses", "none")["reasoning"],
            {"effort": "none"},
        )
        self.assertEqual(
            self.capture_payload("responses", "ultra")["reasoning"],
            {"effort": "ultra"},
        )

    def test_chat_completions_preserves_max(self):
        self.assertEqual(self.capture_payload("chat_completions", "max")["reasoning_effort"], "max")

    def test_base_session_accepts_extended_and_custom_efforts(self):
        for effort in ("none", "max", "ultra", "provider_future_level"):
            session = llmcore.BaseSession({
                "apikey": "test-key",
                "apibase": "https://example.test/v1",
                "model": "test-model",
                "reasoning_effort": effort,
            })
            self.assertEqual(session.reasoning_effort, effort)


if __name__ == "__main__":
    unittest.main()
