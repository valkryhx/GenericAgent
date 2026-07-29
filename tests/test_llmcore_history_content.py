import sys
import unittest
import unittest.mock
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from llmcore import ClaudeSession, NativeClaudeSession  # noqa: E402


CFG = {"apikey": "sk-test-not-a-real-key", "apibase": "https://example.test/v1", "model": "claude-opus-5"}


class StringContentHistoryTest(unittest.TestCase):
    """A history row whose content is a plain string must not be shredded into characters.

    Found by the real-API subagent E2E: `resume_agent` writes `_history.json` from
    `SubagentTranscriptStore.build_resume_context()`, which emits rows shaped
    `{"role": "user", "content": "<text>"}` — content is a **str**, not a block list.
    `BaseSession.ask()` and `ClaudeSession.make_messages()` both do `list(m["content"])`
    assuming block lists, so the string became a list of 1-char strings; the cache_control
    stamping then hit `dict('s', cache_control=...)` and the resumed turn died with
    `ValueError: dictionary update sequence element #0 has length 1; 2 is required`.
    Every resumed subagent turn on an Anthropic-wire backend failed this way.
    """

    def _capture(self, session):
        captured = {}

        def fake_raw_ask(messages):
            captured["messages"] = messages
            yield ""
            return [{"type": "text", "text": "ok"}]

        session.raw_ask = fake_raw_ask
        return captured

    def test_ask_keeps_a_string_history_row_as_one_text_block(self):
        session = NativeClaudeSession(dict(CFG))
        session.history = [{"role": "user", "content": "continue analysis"}]
        captured = self._capture(session)

        list(session.ask({"role": "assistant", "content": [{"type": "text", "text": "prior"}]}))

        first = captured["messages"][0]
        self.assertEqual(first["content"], [{"type": "text", "text": "continue analysis"}])

    def test_ask_does_not_split_a_string_into_per_character_blocks(self):
        session = NativeClaudeSession(dict(CFG))
        session.history = [{"role": "user", "content": "abc"}]
        captured = self._capture(session)

        list(session.ask({"role": "assistant", "content": [{"type": "text", "text": "prior"}]}))

        self.assertNotEqual(len(captured["messages"][0]["content"]), 3, "content was shredded per character")

    def test_make_messages_stamps_cache_control_on_a_string_content_row(self):
        session = ClaudeSession(dict(CFG))

        msgs = session.make_messages([{"role": "user", "content": "continue analysis"}])

        self.assertEqual(msgs[0]["content"][-1]["type"], "text")
        self.assertEqual(msgs[0]["content"][-1]["text"], "continue analysis")
        self.assertEqual(msgs[0]["content"][-1]["cache_control"], {"type": "ephemeral"})

    def test_a_resume_shaped_history_survives_the_full_native_claude_payload_build(self):
        """The exact shape resume_agent writes, through the code path that actually crashed."""
        session = NativeClaudeSession(dict(CFG))
        session.tools = []
        session.history = [
            {"role": "user", "content": "first task"},
            {"role": "assistant", "content": "first answer"},
        ]
        captured = self._capture(session)

        list(session.ask({"role": "user", "content": "second task"}))

        for message in captured["messages"]:
            self.assertIsInstance(message["content"], list)
            for block in message["content"]:
                self.assertIsInstance(block, dict, f"non-dict content block: {block!r}")

    def test_block_list_history_is_left_alone(self):
        """The normal path must keep behaving identically; only str content is coerced."""
        session = NativeClaudeSession(dict(CFG))
        blocks = [{"type": "text", "text": "hello"}, {"type": "text", "text": "world"}]
        session.history = [{"role": "user", "content": blocks}]
        captured = self._capture(session)

        list(session.ask({"role": "assistant", "content": [{"type": "text", "text": "prior"}]}))

        self.assertEqual(captured["messages"][0]["content"], blocks)
        self.assertIsNot(captured["messages"][0]["content"], blocks, "must still be a copy, not the live list")


    def test_native_raw_ask_normalizes_string_content_at_the_crash_site(self):
        """raw_ask is the line that actually raised, and _fix_messages does not wrap str content.

        ask() now normalizes, but raw_ask is a public entry point (MixinSession and the
        cancel/retry paths call it), so the cache_control stamping must not assume block
        lists either. This is the traceback from the real E2E, verbatim:
        `ValueError: dictionary update sequence element #0 has length 1; 2 is required
        @ llmcore.py:842, raw_ask`.
        """
        import llmcore

        session = NativeClaudeSession(dict(CFG))
        session.tools = []
        captured = {}

        def fake_stream(sess, url, headers, payload, parse_fn):
            captured["payload"] = payload
            yield ""
            return [{"type": "text", "text": "ok"}]

        with unittest.mock.patch.object(llmcore, "_stream_with_retry", fake_stream):
            list(session.raw_ask([{"role": "user", "content": "resume shaped row"}]))

        blocks = captured["payload"]["messages"][0]["content"]
        self.assertEqual([b.get("text") for b in blocks], ["resume shaped row"])
        self.assertEqual(blocks[-1]["cache_control"], {"type": "ephemeral"})


if __name__ == "__main__":
    unittest.main()
