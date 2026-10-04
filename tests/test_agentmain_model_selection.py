import types
import unittest

from agentmain import GenericAgent, resolve_reasoning_effort_for_model


class FakeBackend:
    def __init__(self, name, model):
        self.name = name
        self.model = model
        self.history = []


class FakeClient:
    def __init__(self, name, model):
        self.backend = FakeBackend(name, model)
        self.last_tools = "old-tools"


class ModelSelectionTests(unittest.TestCase):
    def make_agent(self):
        agent = GenericAgent.__new__(GenericAgent)
        agent.llm_no = 0
        agent.llmclients = [
            FakeClient("gpt-native", "gpt-5.5"),
            FakeClient("kimi-native", "moonshotai/kimi-k2.6"),
            FakeClient("deepseek", "deepseek-v4"),
        ]
        agent.llmclient = agent.llmclients[0]
        agent.llmclient.backend.history = [{"role": "user", "content": "hi"}]
        agent.load_llm_sessions = types.MethodType(lambda self: None, agent)
        return agent

    def test_select_model_by_index_preserves_history(self):
        agent = self.make_agent()

        result = agent.select_llm("1")

        self.assertTrue(result["ok"])
        self.assertEqual(agent.llm_no, 1)
        self.assertEqual(agent.llmclient.backend.history, [{"role": "user", "content": "hi"}])
        self.assertEqual(agent.llmclient.last_tools, "")

    def test_select_model_by_unique_name_fragment(self):
        agent = self.make_agent()

        result = agent.select_llm("kimi")

        self.assertTrue(result["ok"])
        self.assertEqual(agent.llm_no, 1)

    def test_select_model_reports_ambiguous_fragment(self):
        agent = self.make_agent()

        result = agent.select_llm("native")

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "ambiguous")

    def test_select_model_prefers_exact_name_over_variant_fragment(self):
        agent = self.make_agent()
        agent.llmclients = [
            FakeClient("gpt-6-luna", "gpt-6-luna"),
            FakeClient("gpt-6-luna-chat", "gpt-6-luna-chat"),
        ]
        agent.llmclient = agent.llmclients[0]

        result = agent.select_llm("gpt-6-luna")

        self.assertTrue(result["ok"])
        self.assertEqual(result["index"], 0)
        self.assertEqual(result["model"], "gpt-6-luna")

    def test_select_model_prefers_exact_profile_when_variants_share_api_model(self):
        agent = self.make_agent()
        agent.llmclients = [
            FakeClient("deepseek-v4.1-flash", "deepseek-v4.1-flash"),
            FakeClient("deepseek-v4.1-flash-chat", "deepseek-v4.1-flash"),
        ]
        agent.llmclient = agent.llmclients[0]

        result = agent.select_llm("deepseek-v4.1-flash")

        self.assertTrue(result["ok"])
        self.assertEqual(result["index"], 0)
        self.assertEqual(result["name"], "deepseek-v4.1-flash/deepseek-v4.1-flash")

    def test_select_model_reports_missing_selector(self):
        agent = self.make_agent()

        result = agent.select_llm("not-found")

        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "not_found")

    def make_reasoning_agent(self):
        agent = GenericAgent.__new__(GenericAgent)
        agent.llm_no = 0
        agent.is_running = False
        agent.llmclients = [
            FakeClient("gpt-6-luna", "gpt-6-luna"),
            FakeClient("deepseek-v4.1-flash", "deepseek-v4.1-flash"),
        ]
        luna = agent.llmclients[0].backend
        luna.reasoning_efforts = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]
        luna.reasoning_capabilities_known = True
        luna.default_reasoning_effort = "medium"
        luna.reasoning_effort = "medium"
        deepseek = agent.llmclients[1].backend
        deepseek.reasoning_efforts = ["none", "low", "high", "max"]
        deepseek.reasoning_capabilities_known = True
        deepseek.default_reasoning_effort = "high"
        deepseek.reasoning_effort = "high"
        agent.llmclient = agent.llmclients[0]
        agent.load_llm_sessions = types.MethodType(lambda self: None, agent)
        return agent

    def test_reasoning_resolution_preserves_supported_effort(self):
        self.assertEqual(
            resolve_reasoning_effort_for_model("low", ["low", "high"], "high"),
            "low",
        )

    def test_reasoning_resolution_falls_back_to_default_then_first(self):
        self.assertEqual(
            resolve_reasoning_effort_for_model("ultra", ["low", "high"], "high"),
            "high",
        )
        self.assertEqual(
            resolve_reasoning_effort_for_model("ultra", ["low", "high"], "medium"),
            "low",
        )

    def test_switch_model_applies_target_default_when_current_is_unsupported(self):
        agent = self.make_reasoning_agent()
        agent.llmclient.backend.reasoning_effort = "ultra"
        result = agent.select_llm("deepseek-v4.1-flash")
        self.assertTrue(result["ok"])
        self.assertEqual(result["reasoning_effort"], "high")
        self.assertEqual(agent.llmclient.backend.reasoning_effort, "high")

    def test_switch_model_with_explicit_unsupported_effort_is_rejected(self):
        agent = self.make_reasoning_agent()
        result = agent.select_llm("deepseek-v4.1-flash", reasoning_effort="ultra")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "unsupported_reasoning_effort")
        self.assertEqual(agent.llm_no, 0)

    def test_select_reasoning_effort_updates_current_backend(self):
        agent = self.make_reasoning_agent()
        result = agent.select_reasoning_effort("max")
        self.assertTrue(result["ok"])
        self.assertEqual(agent.llmclient.backend.reasoning_effort, "max")

    def test_select_reasoning_effort_rejects_unknown_value(self):
        agent = self.make_reasoning_agent()
        result = agent.select_reasoning_effort("persistent")
        self.assertFalse(result["ok"])
        self.assertEqual(result["code"], "unsupported_reasoning_effort")

    def test_list_llm_descriptors_exposes_model_specific_capabilities(self):
        agent = self.make_reasoning_agent()
        descriptors = agent.list_llm_descriptors()
        self.assertEqual(descriptors[0]["reasoningEfforts"][-1], "max")
        self.assertEqual(descriptors[1]["reasoningEfforts"], ["none", "low", "high", "max"])
        self.assertTrue(descriptors[0]["current"])


if __name__ == "__main__":
    unittest.main()
