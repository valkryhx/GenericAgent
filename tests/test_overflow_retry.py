"""Overflow inside a turn: raise, roll back, compact, retry once.

The tool loop used to see a context-overflow rejection as ordinary assistant
text (``!!!Error: ...``), so the only retry granularity was "re-submit the whole
user turn" -- which duplicated the user message, re-fired UserPromptSubmit and
could replay side-effecting tools. These tests pin the in-turn contract.
"""

import sys
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from genericagent.core import loop as agent_loop  # noqa: E402
from genericagent.core.errors import ContextOverflowError, is_context_overflow_error  # noqa: E402


class _Response:
    def __init__(self, content="ok"):
        self.content = content
        self.tool_calls = None


class FakeBackend:
    def __init__(self):
        self.history = [{"role": "user", "content": [{"type": "text", "text": "earlier"}]}]
        self.last_provider_error = ""
        self.last_usage_tokens = None


class FakeClient:
    """chat() appends a user message like BaseSession.ask does, then either raises or answers."""

    def __init__(self, outcomes):
        self.backend = FakeBackend()
        self.last_tools = "TOOLS"
        self._pending_tool_ids = []
        self.outcomes = list(outcomes)
        self.calls = 0

    def chat(self, messages, tools=None):
        self.calls += 1
        self.backend.history.append({"role": "user", "content": [{"type": "text", "text": "step %d" % self.calls}]})
        outcome = self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]
        if isinstance(outcome, Exception):
            self.backend.last_provider_error = str(outcome)
            raise outcome
        if outcome == "partial":
            yield "half an answer"
            raise ContextOverflowError("prompt is too long")
        yield "answer text"
        return _Response()


class FakeHandler:
    def __init__(self, parent=None):
        self.parent = parent if parent is not None else types.SimpleNamespace(task_dir=None)
        self.max_turns = 5
        self.current_turn = 0
        self.last_assistant_message = ""
        self._done_hooks = []

    def turn_end_callback(self, *args, **kwargs):
        return ""

    def run_stop_gate(self, exit_reason):
        return ("none", "", None)


def _exiting_tool(*args, **kwargs):
    yield "tool ran\n"
    return types.SimpleNamespace(should_exit=True, data=None, next_prompt="")


class OverflowRetryTest(unittest.TestCase):
    def setUp(self):
        self._original_tool = agent_loop._run_single_tool
        agent_loop._run_single_tool = _exiting_tool
        self.compactions = []
        from genericagent.core import context as compact_context

        self._original_compact = compact_context.compact_agent_context

        def fake_compact(agent, instructions="", **kwargs):
            self.compactions.append(instructions)
            backend = agent.llmclient.backend
            backend.history = [{"role": "user", "content": [{"type": "text", "text": "SUMMARY"}]}]
            return types.SimpleNamespace(ok=True, message="Compacted.", summary="SUMMARY")

        compact_context.compact_agent_context = fake_compact

    def tearDown(self):
        agent_loop._run_single_tool = self._original_tool
        from genericagent.core import context as compact_context

        compact_context.compact_agent_context = self._original_compact

    def _agent(self):
        return types.SimpleNamespace(task_dir=None)

    def _run(self, client, agent):
        agent.llmclient = types.SimpleNamespace(backend=client.backend)
        handler = FakeHandler(parent=agent)
        chunks = list(agent_loop.agent_runner_loop(
            client, "SYSTEM", "hello", handler, [], max_turns=3,
        ))
        return "".join(str(c) for c in chunks if isinstance(c, str))

    def test_one_overflow_is_compacted_and_retried_in_place(self):
        client = FakeClient([ContextOverflowError("context_length_exceeded"), None])
        out = self._run(client, self._agent())
        self.assertEqual(2, client.calls)
        self.assertEqual(1, len(self.compactions))
        self.assertIn("retry", out.lower())
        # the failed attempt's appended user message was rolled back; the retry
        # appended exactly one
        user_msgs = [m for m in client.backend.history if m.get("role") == "user"]
        self.assertEqual(2, len(user_msgs))  # 1 pre-existing + 1 from the retry
        self.assertEqual("", client.backend.last_provider_error)

    def test_a_second_overflow_is_not_retried_again(self):
        client = FakeClient([ContextOverflowError("context_length_exceeded")])
        with self.assertRaises(ContextOverflowError):
            self._run(client, self._agent())
        self.assertEqual(2, client.calls)

    def test_other_errors_are_not_retried(self):
        client = FakeClient([RuntimeError("upstream access forbidden")])
        with self.assertRaises(RuntimeError):
            self._run(client, self._agent())
        self.assertEqual(1, client.calls)
        self.assertEqual([], self.compactions)

    def test_a_normal_turn_does_not_compact(self):
        client = FakeClient([None])
        self._run(client, self._agent())
        self.assertEqual(1, client.calls)
        self.assertEqual([], self.compactions)

    def test_partial_output_is_marked_discarded_before_the_retry(self):
        client = FakeClient(["partial", None])
        out = self._run(client, self._agent())
        self.assertEqual(2, client.calls)
        self.assertIn("half an answer", out)
        self.assertIn("discarded", out)
        # the retry's own answer is what the turn commits
        self.assertIn("answer text", out)


class ParserRaisesOverflowTest(unittest.TestCase):
    def test_openai_error_payload_raises_overflow(self):
        from genericagent.llm import core as llm_core

        with self.assertRaises(ContextOverflowError):
            list(llm_core._parse_openai_json(
                {"error": {"message": "maximum context length is 200000 tokens"}}, sess=None))

    def test_non_overflow_payload_stays_a_text_error(self):
        from genericagent.llm import core as llm_core

        chunks = list(llm_core._parse_openai_json(
            {"error": {"message": "upstream access forbidden"}}, sess=None))
        self.assertTrue(any("!!!Error" in str(c) for c in chunks))


class MatcherTest(unittest.TestCase):
    def test_matches_and_rejects(self):
        self.assertTrue(is_context_overflow_error("context_length_exceeded"))
        self.assertTrue(is_context_overflow_error("prompt is too long"))
        self.assertTrue(is_context_overflow_error("413 Request Entity Too Large"))
        self.assertFalse(is_context_overflow_error("upstream access forbidden"))
        self.assertFalse(is_context_overflow_error(""))


if __name__ == "__main__":
    unittest.main()
