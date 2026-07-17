import copy
import ast
import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from compact_context import compact_agent_context, replace_log_with_compact_history, should_auto_compact_agent  # noqa: E402
from compact_context import DEFAULT_CONTEXT_WIN, _source_char_budget  # noqa: E402


class FakeBackend:
    def __init__(self):
        self.context_win = 100
        self.history = [
            {"role": "user", "content": [{"type": "text", "text": "first user asked for alpha"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "first answer mentioned beta"}]},
            {"role": "user", "content": [{"type": "text", "text": "second user asked for gamma"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "second answer mentioned delta"}]},
        ]


class FakeClient:
    def __init__(self):
        self.backend = FakeBackend()
        self.last_tools = "cached tools"


class FakeAgent:
    def __init__(self):
        self.history = ["[USER]: first", "[Agent] first answer"]
        self.handler = object()
        self.llmclient = FakeClient()
        self.aborted = False

    def abort(self):
        self.aborted = True


class FakeNativeBackend:
    def __init__(self):
        self.context_win = 100
        self.history = [
            {"role": "user", "content": [{"type": "text", "text": "native user context"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "native assistant context"}]},
        ]

    def ask(self, msg):
        assert isinstance(msg, dict)
        yield "native summary"


class FakeMixinBackend:
    def __init__(self):
        self.primary = FakeNativeBackend()

    def __getattr__(self, name):
        return getattr(self.primary, name)

    def __setattr__(self, name, value):
        if name == "history" and "primary" in self.__dict__:
            self.primary.history = value
        else:
            object.__setattr__(self, name, value)


class FakeMixinNativeClient:
    def __init__(self):
        self.backend = FakeMixinBackend()
        self.last_tools = "cached tools"


class FakeMixinNativeAgent(FakeAgent):
    def __init__(self):
        super().__init__()
        self.llmclient = FakeMixinNativeClient()


class CompactContextTest(unittest.TestCase):
    def test_manual_compact_replaces_backend_history_with_summary_message(self):
        agent = FakeAgent()

        result = compact_agent_context(
            agent,
            summarize_fn=lambda source, instructions: "Summary keeps alpha, beta, gamma, and delta.",
        )

        self.assertTrue(result.ok)
        self.assertEqual(4, result.original_messages)
        self.assertEqual(2, len(agent.llmclient.backend.history))
        compacted = agent.llmclient.backend.history[0]
        self.assertEqual("user", compacted["role"])
        self.assertIn("Summary keeps alpha", compacted["content"][0]["text"])
        self.assertIn("<compact_summary>", compacted["content"][0]["text"])
        self.assertEqual("assistant", agent.llmclient.backend.history[1]["role"])
        self.assertEqual(["[Agent] Compacted context: Summary keeps alpha, beta, gamma, and delta."], agent.history)
        self.assertEqual("", agent.llmclient.last_tools)
        self.assertIsNone(agent.handler)

    def test_manual_compact_failure_preserves_existing_state(self):
        agent = FakeAgent()
        old_history = copy.deepcopy(agent.llmclient.backend.history)
        old_agent_history = copy.deepcopy(agent.history)

        result = compact_agent_context(
            agent,
            summarize_fn=lambda _source, _instructions: (_ for _ in ()).throw(RuntimeError("boom")),
        )

        self.assertFalse(result.ok)
        self.assertEqual("boom", result.message)
        self.assertEqual(old_history, agent.llmclient.backend.history)
        self.assertEqual(old_agent_history, agent.history)
        self.assertEqual("cached tools", agent.llmclient.last_tools)

    def test_manual_compact_failure_reports_exception_type_when_message_is_empty(self):
        agent = FakeAgent()

        result = compact_agent_context(
            agent,
            summarize_fn=lambda _source, _instructions: (_ for _ in ()).throw(AssertionError()),
        )

        self.assertFalse(result.ok)
        self.assertEqual("AssertionError", result.message)

    def test_manual_compact_sends_dict_prompt_to_mixin_wrapped_native_backend(self):
        agent = FakeMixinNativeAgent()

        result = compact_agent_context(agent)

        self.assertTrue(result.ok)
        self.assertIn("native summary", agent.llmclient.backend.history[0]["content"][0]["text"])

    def test_auto_compact_soft_line_derived_from_context_win(self):
        # 无显式 auto_compact_tokens → 软线从 context_win × 0.90 派生（对齐 Codex）。
        agent = FakeAgent()
        agent.llmclient.backend.context_win = 10_000  # 软线 9000
        # 冷启动全量估算，短历史 + 短 pending 远低于软线
        self.assertFalse(should_auto_compact_agent(agent, pending_text="short"))

        agent.llmclient.backend.context_win = 20  # 软线 18
        self.assertTrue(should_auto_compact_agent(agent, pending_text="x" * 200))

    def test_auto_compact_default_soft_line_uses_400k_window(self):
        # 连 context_win 都没有 → 用 DEFAULT_CONTEXT_WIN，软线 = 400k × 0.90 = 360k tokens。
        agent = FakeAgent()
        delattr(agent.llmclient.backend, "context_win")
        self.assertEqual(400_000, DEFAULT_CONTEXT_WIN)
        # 冷启动按 chars/4 估算：pending 100 万字符 ≈ 25 万 token < 36 万 → 不触发
        self.assertFalse(should_auto_compact_agent(agent, pending_text="x" * 1_000_000))
        # 160 万字符 ≈ 40 万 token > 36 万 → 触发
        self.assertTrue(should_auto_compact_agent(agent, pending_text="x" * 1_600_000))

    def test_auto_compact_prefers_explicit_soft_limit(self):
        # backend 有显式 auto_compact_tokens（配置层派生）→ 直接用它当软线，
        # 不再从 context_win 派生。
        agent = FakeAgent()
        agent.llmclient.backend.context_win = 1_000_000  # 大窗口，证明不被采用
        agent.llmclient.backend.auto_compact_tokens = 50
        # 冷启动短历史 ~27 token < 50 → 不触发
        self.assertFalse(should_auto_compact_agent(agent, pending_text=""))
        # + pending 400 字符 ≈ 100 token → 超 50 → 触发
        self.assertTrue(should_auto_compact_agent(agent, pending_text="x" * 400))

    def test_auto_compact_uses_real_token_baseline(self):
        # 有 last_usage_tokens（真实 token）→ 用它当基准（history 末尾是 assistant，
        # 其后无新增，delta=0）。软线 900。
        agent = FakeAgent()
        agent.llmclient.backend.auto_compact_tokens = 900
        agent.llmclient.backend.last_usage_tokens = {
            "input_tokens": 850, "output_tokens": 0, "total_tokens": 850,
        }
        # 850 + 0 = 850 < 900 → 不触发
        self.assertFalse(should_auto_compact_agent(agent, pending_text=""))
        # 850 + 400/4=100 = 950 > 900 → 触发
        self.assertTrue(should_auto_compact_agent(agent, pending_text="x" * 400))

    def test_auto_compact_real_token_ignores_char_estimate_magnitude(self):
        # 真实 token 路径下，即便历史字符数很大（旧字符口径会误判），只要真实 token 低
        # 就不触发——证明确实用 token 而非字符。history 末尾放 assistant 走真实基准路径。
        agent = FakeAgent()
        agent.llmclient.backend.auto_compact_tokens = 90_000
        agent.llmclient.backend.last_usage_tokens = {
            "input_tokens": 10, "output_tokens": 10, "total_tokens": 20,
        }
        agent.llmclient.backend.history = [
            {"role": "user", "content": [{"type": "text", "text": "x" * 500_000}]},
            {"role": "assistant", "content": [{"type": "text", "text": "ok"}]},
        ]
        # 真实基准 20 + delta 0 = 20 << 90000 → 不触发（字符估算会是 ~12 万 token）
        self.assertFalse(should_auto_compact_agent(agent, pending_text=""))

    def test_auto_compact_cold_start_full_char_estimate(self):
        # 无 last_usage_tokens（从未成功请求）→ 冷启动全量字符估算（chars/4）。
        agent = FakeAgent()
        agent.llmclient.backend.context_win = 20  # 软线 18
        # 短历史 ~27 token > 18 → 触发
        self.assertTrue(should_auto_compact_agent(agent, pending_text=""))

    def test_auto_compact_total_derived_from_input_output(self):
        # last_usage_tokens 只有 input/output、没 total_tokens → 由二者求和当基准。
        agent = FakeAgent()
        agent.llmclient.backend.auto_compact_tokens = 900
        agent.llmclient.backend.last_usage_tokens = {
            "input_tokens": 500, "output_tokens": 450,
        }
        # 500+450=950 > 900（history 末尾 assistant，delta=0）→ 触发
        self.assertTrue(should_auto_compact_agent(agent, pending_text=""))

    def test_compact_source_budget_scales_for_large_default_context(self):
        backend = type("Backend", (), {})()

        self.assertEqual(800_000, _source_char_budget(backend))

    def test_replace_log_with_compact_history_archives_old_log_and_writes_restorable_pair(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "model_responses_123.txt"
            log_path.write_text("=== Prompt === old\nold prompt\n\n=== Response === old\nold response\n\n", encoding="utf-8")
            history = [
                {"role": "user", "content": [{"type": "text", "text": "<compact_summary>\nkeep alpha\n</compact_summary>"}]},
                {"role": "assistant", "content": [{"type": "text", "text": "<summary>loaded compact context</summary>"}]},
            ]

            snapshot = replace_log_with_compact_history(str(log_path), history)
            content = log_path.read_text(encoding="utf-8")
            self.assertIsNotNone(snapshot)
            self.assertTrue(Path(snapshot).exists())

        self.assertIn("=== Prompt ===", content)
        self.assertIn("=== Response ===", content)
        prompt = content.split("=== Prompt ===", 1)[1].split("\n", 1)[1].split("\n\n=== Response ===", 1)[0].strip()
        response = content.split("=== Response ===", 1)[1].split("\n", 1)[1].strip()
        self.assertEqual(history[0], json.loads(prompt))
        self.assertEqual(history[1]["content"], ast.literal_eval(response))


if __name__ == "__main__":
    unittest.main()
