"""压缩优化的契约测试（P0 四项）。

1 压缩后保留最近一轮原文（对齐 CC messagesToKeep）；
2 压缩后把 handler working 状态重新注入并回填到新 handler；
3 自动压缩从 ink 前端下沉到核心，CLI 也能触发，且有熔断；
4 微压缩只清 tool_result、有节省门槛、幂等。
"""

import json
import queue
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from genericagent.core import context as compact_context  # noqa: E402
from genericagent.core.app import GeneraticAgent  # noqa: E402


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
    def __init__(self, history, working=None):
        self.llmclient = FakeClient(FakeBackend(history))
        self.history = []
        self.handler = FakeHandler(working)


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


def _dump(messages):
    return json.dumps(messages, ensure_ascii=False)


class RecentTurnPreservationTest(unittest.TestCase):
    def test_the_latest_turn_survives_verbatim(self):
        agent = FakeAgent(_conversation())
        result = compact_context.compact_agent_context(agent, summarize_fn=lambda src, ins: "SUMMARY")
        self.assertTrue(result.ok, result.message)
        dumped = _dump(agent.llmclient.backend.history)
        self.assertIn("latest request LAMBDA", dumped)
        self.assertIn("latest tool output OMEGA", dumped)
        self.assertIn("latest answer SIGMA", dumped)
        self.assertNotIn("old request ALPHA", dumped)
        self.assertNotIn("old tool output BETA", dumped)
        self.assertIn("SUMMARY", _dump(agent.llmclient.backend.history[:2]))
        self.assertGreater(result.kept_messages, 0)

    def test_a_partial_turn_is_kept_too(self):
        # Compaction can happen mid-turn (the tail ends with a tool result);
        # that in-flight work is exactly what must survive.
        agent = FakeAgent(_conversation()[:7])
        result = compact_context.compact_agent_context(agent, summarize_fn=lambda src, ins: "SUMMARY")
        self.assertTrue(result.ok)
        self.assertEqual(3, result.kept_messages)
        self.assertEqual(5, len(agent.llmclient.backend.history))
        self.assertIn("latest tool output OMEGA", _dump(agent.llmclient.backend.history[-1]))

    def test_keep_recent_turns_zero_restores_the_old_shape(self):
        agent = FakeAgent(_conversation())
        result = compact_context.compact_agent_context(
            agent, summarize_fn=lambda src, ins: "SUMMARY", keep_recent_turns=0
        )
        self.assertEqual(0, result.kept_messages)
        self.assertEqual(2, len(agent.llmclient.backend.history))

    def test_orphan_tool_result_at_the_start_of_the_tail_is_dropped(self):
        agent = FakeAgent(_conversation())
        compact_context.compact_agent_context(agent, summarize_fn=lambda src, ins: "SUMMARY")
        history = agent.llmclient.backend.history
        first_kept = history[2]
        self.assertEqual("user", first_kept["role"])
        types = [block.get("type") for block in first_kept["content"]]
        self.assertNotIn("tool_result", types)


class AutoCompactHookTest(unittest.TestCase):
    def _agent(self):
        agent = FakeAgent(_conversation())
        agent.llmclient.backend.context_win = 100
        agent.llmclient.backend.auto_compact_tokens = 10
        return agent

    def test_a_blocking_precompact_hook_vetoes_compaction(self):
        agent = self._agent()

        class Blocked:
            blocked = True
            reason = "nope"

        original = compact_context._run_compact_hook
        compact_context._run_compact_hook = lambda a, event, payload: Blocked() if event == "PreCompact" else None
        try:
            self.assertIsNone(compact_context.maybe_auto_compact(agent, pending_text="q"))
        finally:
            compact_context._run_compact_hook = original
        self.assertNotIn("<compact_summary>", _dump(agent.llmclient.backend.history))
        self.assertEqual(0, getattr(agent, "_auto_compact_failures", 0))

    def test_postcompact_hook_runs_with_the_auto_trigger(self):
        agent = self._agent()
        seen = []
        original = compact_context._run_compact_hook

        def fake(a, event, payload):
            seen.append((event, payload.get("trigger")))
            return None

        compact_context._run_compact_hook = fake
        try:
            self.assertTrue(compact_context.maybe_auto_compact(agent, pending_text="q"))
        finally:
            compact_context._run_compact_hook = original
        self.assertEqual([("PreCompact", "auto"), ("PostCompact", "auto")], seen)

    def test_the_compaction_is_recorded_in_the_transcript(self):
        agent = self._agent()
        recorded = []
        original = compact_context.record_compact_transcript
        compact_context.record_compact_transcript = lambda a, result, trigger: recorded.append(trigger)
        try:
            self.assertTrue(compact_context.maybe_auto_compact(agent, pending_text="q"))
        finally:
            compact_context.record_compact_transcript = original
        self.assertEqual(["auto"], recorded)


class UsageAnchorResetTest(unittest.TestCase):
    def test_a_compacted_history_does_not_re_trigger_on_stale_usage(self):
        # The last real usage described the pre-compaction history (huge). If it
        # survived compaction, the very next request would compact again -- a
        # summary of the summary.
        agent = FakeAgent(_conversation())
        agent.llmclient.backend.last_usage_tokens = {"total_tokens": 900_000}
        agent.llmclient.backend.auto_compact_tokens = 10_000
        self.assertTrue(compact_context.should_auto_compact_agent(agent, pending_text="next"))
        compact_context.compact_agent_context(agent, summarize_fn=lambda src, ins: "SUMMARY")
        self.assertFalse(compact_context.should_auto_compact_agent(agent, pending_text="next"))


class WorkingStateRestoreTest(unittest.TestCase):
    WORKING = {
        "key_info": "external idea XI",
        "related_sop": "sop-1",
        "active_skill": "demo-skill",
        "in_plan_mode": "C:/plans/plan.md",
        "passed_sessions": 2,
    }

    def test_working_state_is_reinjected_into_the_summary(self):
        agent = FakeAgent(_conversation(), working=self.WORKING)
        result = compact_context.compact_agent_context(agent, summarize_fn=lambda src, ins: "SUMMARY")
        self.assertTrue(result.ok)
        dumped = _dump(agent.llmclient.backend.history)
        self.assertIn("external idea XI", dumped)
        self.assertIn("demo-skill", dumped)
        self.assertIn("<working_state>", dumped)

    def test_working_state_is_stashed_for_the_next_handler(self):
        agent = FakeAgent(_conversation(), working=self.WORKING)
        compact_context.compact_agent_context(agent, summarize_fn=lambda src, ins: "SUMMARY")
        stashed = getattr(agent, "_compact_working_state", None)
        self.assertIsNotNone(stashed)
        self.assertEqual("external idea XI", stashed["handler_working"]["key_info"])

    def test_restore_applies_the_stash_once(self):
        agent = FakeAgent(_conversation(), working=self.WORKING)
        compact_context.compact_agent_context(agent, summarize_fn=lambda src, ins: "SUMMARY")
        fresh = FakeHandler()
        compact_context.restore_compact_working_state(agent, fresh)
        self.assertEqual("external idea XI", fresh.working["key_info"])
        self.assertEqual("demo-skill", fresh.working["active_skill"])
        another = FakeHandler()
        compact_context.restore_compact_working_state(agent, another)
        self.assertEqual({}, another.working)

    def test_no_stash_when_there_is_no_working_state(self):
        agent = FakeAgent(_conversation(), working={})
        compact_context.compact_agent_context(agent, summarize_fn=lambda src, ins: "SUMMARY")
        self.assertIsNone(getattr(agent, "_compact_working_state", None))


class CoreAutoCompactTest(unittest.TestCase):
    def _agent(self):
        agent = FakeAgent(_conversation())
        agent.llmclient.backend.context_win = 100
        agent.llmclient.backend.auto_compact_tokens = 10
        return agent

    def test_maybe_auto_compact_returns_a_message_when_it_compacts(self):
        agent = self._agent()
        message = compact_context.maybe_auto_compact(agent, pending_text="new question")
        self.assertTrue(message)
        self.assertIn("<compact_summary>", _dump(agent.llmclient.backend.history))

    def test_maybe_auto_compact_is_a_noop_below_the_soft_line(self):
        agent = self._agent()
        agent.llmclient.backend.auto_compact_tokens = 10_000_000
        self.assertIsNone(compact_context.maybe_auto_compact(agent, pending_text="new question"))

    def test_breaker_stops_after_three_consecutive_failures(self):
        agent = self._agent()

        def boom(src, ins):
            raise RuntimeError("summarizer down")

        for _ in range(3):
            self.assertIsNone(compact_context.maybe_auto_compact(agent, pending_text="q", summarize_fn=boom))
        self.assertEqual(3, agent._auto_compact_failures)
        calls = []

        def counted(src, ins):
            calls.append(1)
            return "SUMMARY"

        self.assertIsNone(compact_context.maybe_auto_compact(agent, pending_text="q", summarize_fn=counted))
        self.assertEqual([], calls)

    def test_a_success_resets_the_breaker(self):
        agent = self._agent()
        agent._auto_compact_failures = 2
        self.assertTrue(compact_context.maybe_auto_compact(agent, pending_text="q"))
        self.assertEqual(0, agent._auto_compact_failures)


class AutoCompactEntryTest(unittest.TestCase):
    """自动压缩必须在"任务消费者线程"里、回合开始前发生（不能卡 UI 线程）。"""

    def _fake_app_agent(self):
        import types

        agent = FakeAgent(_conversation())
        agent.task_queue = queue.Queue()
        agent.is_running = False
        agent._auto_compact_before_task = types.MethodType(
            GeneraticAgent._auto_compact_before_task, agent
        )
        return agent

    def test_the_hook_compacts_and_reports_before_the_turn(self):
        agent = self._fake_app_agent()
        seen = {}

        def fake_maybe(target, pending_text="", **kwargs):
            seen["pending"] = pending_text
            return "Auto compacted 4 messages into summary context."

        original = compact_context.maybe_auto_compact
        compact_context.maybe_auto_compact = fake_maybe
        try:
            out = queue.Queue()
            agent._auto_compact_before_task("please do X", out)
        finally:
            compact_context.maybe_auto_compact = original
        self.assertEqual("please do X", seen.get("pending"))
        first = out.get_nowait()
        self.assertIn("Auto compacted", first.get("done", ""))

    def test_slash_commands_are_ignored(self):
        agent = self._fake_app_agent()
        seen = []
        original = compact_context.maybe_auto_compact

        def fake_maybe(target, pending_text="", **kwargs):
            seen.append(pending_text)
            return None

        compact_context.maybe_auto_compact = fake_maybe
        try:
            agent._auto_compact_before_task("/compact now", queue.Queue())
        finally:
            compact_context.maybe_auto_compact = original
        self.assertEqual([], seen)

    def test_a_running_agent_is_left_alone(self):
        agent = self._fake_app_agent()
        agent.is_running = True
        seen = []
        original = compact_context.maybe_auto_compact

        def fake_maybe(target, pending_text="", **kwargs):
            seen.append(pending_text)
            return None

        compact_context.maybe_auto_compact = fake_maybe
        try:
            agent._auto_compact_before_task("queued follow-up", queue.Queue())
        finally:
            compact_context.maybe_auto_compact = original
        self.assertEqual([], seen)

    def test_the_task_loop_actually_calls_the_hook(self):
        # AST, not text matching: the call must live inside ``run``.
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(GeneraticAgent.run).lstrip())
        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                found.append(node.func.attr)
        self.assertIn("_auto_compact_before_task", found)


class MicrocompactTest(unittest.TestCase):
    def setUp(self):
        from genericagent.llm import core as llm_core

        self.llm_core = llm_core

    def _history(self, result_chars=4000, tool_calls=4):
        history = [{"role": "user", "content": _text("do the thing")}]
        for index in range(tool_calls):
            history.append({"role": "assistant", "content": [
                _tool_use("t%d" % index, args={"path": "file%d.py" % index, "start": 1})]})
            history.append({"role": "user", "content": [
                _tool_result("t%d" % index, "R" * result_chars)]})
        return history

    def test_below_the_savings_threshold_nothing_changes(self):
        history = self._history(result_chars=50, tool_calls=3)
        before = _dump(history)
        self.llm_core.compress_history_tags(history, keep_recent=1, min_savings_tokens=20000)
        self.assertEqual(before, _dump(history))

    def test_only_old_tool_results_are_cleared(self):
        history = self._history()
        self.llm_core.compress_history_tags(history, keep_recent=1, min_savings_tokens=10)
        dumped = _dump(history)
        # only the newest tool result survives (keep_recent=1)
        self.assertEqual(1, dumped.count("R" * 4000))
        self.assertIn("file0.py", dumped)
        self.assertEqual(4, dumped.count('"tool_use"'))

    def test_clearing_is_idempotent(self):
        history = self._history()
        self.llm_core.compress_history_tags(history, keep_recent=1, min_savings_tokens=10)
        first = _dump(history)
        self.llm_core.compress_history_tags(history, keep_recent=1, min_savings_tokens=10)
        self.assertEqual(first, _dump(history))

    def test_force_ignores_the_threshold(self):
        history = self._history(result_chars=20, tool_calls=3)
        self.llm_core.compress_history_tags(history, keep_recent=0, min_savings_tokens=20000, force=True)
        self.assertNotIn("R" * 20, _dump(history))

    def test_thinking_blocks_are_never_truncated(self):
        history = [
            {"role": "user", "content": _text("do it")},
            {"role": "assistant", "content": [{"type": "thinking", "thinking": "T" * 5000}]},
            {"role": "user", "content": _text("ok")},
        ]
        self.llm_core.compress_history_tags(history, keep_recent=0, min_savings_tokens=1)
        self.assertIn("T" * 5000, _dump(history))


if __name__ == "__main__":
    unittest.main()
