"""partial compact（只压一段）的契约测试。

对齐 CC `Qki`（m0591:115041）：
- direction="up_to"：摘要 history[:pivot]，history[pivot:] 原样保留；
- direction="from" ：history[:pivot] 原样保留，摘要 history[pivot:]；
- pivot 落在 tool_result 上时向后吸附，绝不切断 tool_use / tool_result 配对。
"""

import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from genericagent.core import context as compact_context  # noqa: E402


def _text(value):
    return [{"type": "text", "text": value}]


def _tool_use(call_id, name="file_read", args=None):
    return {"type": "tool_use", "id": call_id, "name": name, "input": args or {"path": "x.py"}}


def _tool_result(call_id, value):
    return {"type": "tool_result", "tool_use_id": call_id, "content": value}


class FakeBackend:
    def __init__(self, history, context_win=1000):
        self.context_win = context_win
        self.history = history
        self.auto_compact_tokens = int(context_win * 0.90)
        self.auto_compact_enabled = True
        self.last_usage_tokens = None
        self.asked = []

    def ask(self, prompt):
        self.asked.append(prompt)
        yield "SUMMARY"


class FakeHandler:
    def __init__(self, working=None):
        self.working = dict(working or {})


class FakeClient:
    def __init__(self, backend):
        self.backend = backend
        self.last_tools = "last tools"


class FakeAgent:
    def __init__(self, history):
        self.llmclient = FakeClient(FakeBackend(history))
        self.history = []
        self.handler = FakeHandler()


def _conversation():
    return [
        {"role": "user", "content": _text("old request ALPHA")},
        {"role": "assistant", "content": [_tool_use("a1")]},
        {"role": "user", "content": [_tool_result("a1", "old tool output BETA")]},
        {"role": "assistant", "content": _text("old answer GAMMA")},
        {"role": "user", "content": _text("latest request LAMBDA")},
        {"role": "assistant", "content": [_tool_use("b1")]},
        {"role": "user", "content": [_tool_result("b1", "latest tool output OMEGA")]},
        {"role": "assistant", "content": _text("latest answer SIGMA")},
    ]


def _summarizer(seen):
    def run(source, instructions):
        seen.append((source, instructions))
        return "PARTIAL SUMMARY"
    return run


class PartialCompactTest(unittest.TestCase):
    def test_from_keeps_prefix_verbatim_and_appends_summary(self):
        agent = FakeAgent(_conversation())
        original = list(agent.llmclient.backend.history)
        seen = []
        result = compact_context.compact_agent_context(
            agent, direction="from", pivot=4, summarize_fn=_summarizer(seen)
        )
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.direction, "from")
        self.assertEqual(result.pivot, 4)
        history = agent.llmclient.backend.history
        self.assertEqual(history[:4], original[:4])
        self.assertIn("GAMMA", str(history[3]))
        self.assertIn("<compact_summary>", str(history[4]))
        self.assertNotIn("ALPHA", seen[0][0])
        self.assertIn("LAMBDA", seen[0][0])
        self.assertEqual(len(history), 6)

    def test_upto_with_pivot_keeps_suffix_verbatim(self):
        agent = FakeAgent(_conversation())
        original = list(agent.llmclient.backend.history)
        seen = []
        result = compact_context.compact_agent_context(
            agent, direction="up_to", pivot=4, summarize_fn=_summarizer(seen)
        )
        self.assertTrue(result.ok, result.message)
        history = agent.llmclient.backend.history
        self.assertEqual(len(history), 6)
        self.assertIn("<compact_summary>", str(history[0]))
        self.assertEqual(history[2:], original[4:])
        self.assertIn("ALPHA", seen[0][0])
        self.assertNotIn("LAMBDA", seen[0][0])
        self.assertEqual(result.kept_messages, 4)

    def test_pivot_snaps_forward_over_tool_results(self):
        agent = FakeAgent(_conversation())
        result = compact_context.compact_agent_context(
            agent, direction="from", pivot=2, summarize_fn=_summarizer([])
        )
        self.assertTrue(result.ok, result.message)
        self.assertEqual(result.pivot, 3)
        history = agent.llmclient.backend.history
        self.assertIn("BETA", str(history[2]))
        self.assertIn("<compact_summary>", str(history[3]))

    def test_invalid_direction_rejected(self):
        agent = FakeAgent(_conversation())
        before = list(agent.llmclient.backend.history)
        result = compact_context.compact_agent_context(
            agent, direction="sideways", pivot=3, summarize_fn=_summarizer([])
        )
        self.assertFalse(result.ok)
        self.assertIn("direction", result.message)
        self.assertEqual(agent.llmclient.backend.history, before)

    def test_pivot_out_of_range_rejected(self):
        agent = FakeAgent(_conversation())
        before = list(agent.llmclient.backend.history)
        result = compact_context.compact_agent_context(
            agent, direction="up_to", pivot=99, summarize_fn=_summarizer([])
        )
        self.assertFalse(result.ok)
        self.assertIn("pivot", result.message)
        self.assertEqual(agent.llmclient.backend.history, before)

    def test_empty_summarized_part_rejected(self):
        agent = FakeAgent(_conversation())
        result = compact_context.compact_agent_context(
            agent, direction="up_to", pivot=0, summarize_fn=_summarizer([])
        )
        self.assertFalse(result.ok)
        self.assertIn("Nothing to summarize", result.message)

    def test_from_prompt_states_prefix_is_kept(self):
        prompt = compact_context._compact_prompt("SRC", "", direction="from")
        self.assertIn("kept verbatim", prompt)
        self.assertIn("later", prompt)
        plain = compact_context._compact_prompt("SRC", "")
        self.assertNotIn("kept verbatim", plain)

    def test_partial_compaction_never_reuses_precomputed_cache(self):
        agent = FakeAgent(_conversation())
        # Put the session over the precompute watermark (0.8 * soft line).
        agent.llmclient.backend.last_usage_tokens = {"total_tokens": 800}
        pre_seen = []
        compact_context.precompute_compaction(agent, summarize_fn=_summarizer(pre_seen))
        self.assertIsNotNone(getattr(agent, "_precomputed_compact", None))
        seen = []
        result = compact_context.compact_agent_context(
            agent, direction="up_to", pivot=4, summarize_fn=_summarizer(seen)
        )
        self.assertTrue(result.ok, result.message)
        self.assertFalse(result.precomputed_reused)
        self.assertEqual(len(seen), 1)
        self.assertIsNone(getattr(agent, "_precomputed_compact", None))


class CompactCommandParseTest(unittest.TestCase):
    def test_plain_instructions(self):
        parsed = compact_context.parse_compact_command("keep decisions")
        self.assertEqual(parsed["action"], "compact")
        self.assertIsNone(parsed["pivot"])
        self.assertEqual(parsed["instructions"], "keep decisions")

    def test_from_with_pivot_and_instructions(self):
        parsed = compact_context.parse_compact_command("--from 3 keep decisions")
        self.assertEqual(parsed["action"], "compact")
        self.assertEqual(parsed["direction"], "from")
        self.assertEqual(parsed["pivot"], 3)
        self.assertEqual(parsed["instructions"], "keep decisions")

    def test_upto_with_pivot(self):
        parsed = compact_context.parse_compact_command("--upto 2")
        self.assertEqual(parsed["direction"], "up_to")
        self.assertEqual(parsed["pivot"], 2)
        self.assertEqual(parsed["instructions"], "")

    def test_list(self):
        parsed = compact_context.parse_compact_command("--list")
        self.assertEqual(parsed["action"], "list")

    def test_flag_without_number_is_an_error(self):
        parsed = compact_context.parse_compact_command("--from")
        self.assertIn("error", parsed)

    def test_describe_history_lists_indexes_and_roles(self):
        agent = FakeAgent(_conversation())
        table = compact_context.describe_history_for_compact(agent)
        self.assertIn("0", table)
        self.assertIn("user", table)
        self.assertIn("assistant", table)
        self.assertIn("ALPHA", table)


if __name__ == "__main__":
    unittest.main()
