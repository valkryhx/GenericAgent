import types
import unittest

from agentmain import GenericAgent


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


if __name__ == "__main__":
    unittest.main()
