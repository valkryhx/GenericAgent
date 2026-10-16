import unittest

from genericagent.agents.team_activation import (
    TeamActivationState,
    agent_teams_env_enabled,
    resolve_team_activation,
    team_activation_reminder,
)


class TeamSignalTest(unittest.TestCase):
    def test_explicit_team_words_are_requested(self):
        for text in ("用 agent team 做", "多智能体协作", "起几个 teammate 并行"):
            with self.subTest(text=text):
                decision = resolve_team_activation(text)
                self.assertEqual(("explicit", "requested"), (decision.mode, decision.action))
                self.assertIn("explicit_team", decision.matched_signals)

    def test_parallel_counted_request_is_recommended(self):
        decision = resolve_team_activation("帮我并行做三件互不相关的小统计，然后汇总成一份报告")
        self.assertEqual(("semantic", "recommended"), (decision.mode, decision.action))
        self.assertIn("counted_fanout", decision.matched_signals)
        self.assertIn("independent", decision.matched_signals)
        self.assertTrue(decision.requires_parallel)

    def test_multi_item_parallel_request_is_recommended(self):
        decision = resolve_team_activation("并行调研 claude-haiku-5.5 ,gpt-6-luna 和 gemini-4-argon")
        self.assertEqual("recommended", decision.action)
        self.assertIn("multi_item", decision.matched_signals)

    def test_semantic_workflow_request_is_not_a_team_signal(self):
        decision = resolve_team_activation("先搜索多个来源，再生成 HTML 文件并验证")
        self.assertEqual(("none", "none"), (decision.mode, decision.action))

    def test_weak_or_plain_requests_do_not_fire(self):
        self.assertEqual("none", resolve_team_activation("同时也要注意性能").action)
        self.assertEqual("none", resolve_team_activation("explain MCP").action)
        self.assertEqual("none", resolve_team_activation("").action)


class TeamSessionTest(unittest.TestCase):
    def test_env_gate(self):
        self.assertTrue(agent_teams_env_enabled({"GA_AGENT_TEAMS": "1"}))
        self.assertTrue(agent_teams_env_enabled({"GA_AGENT_TEAMS": "ON"}))
        self.assertFalse(agent_teams_env_enabled({}))
        self.assertFalse(agent_teams_env_enabled({"GA_AGENT_TEAMS": "0"}))

    def test_session_switch_and_substantive_turns(self):
        state = TeamActivationState(enabled=False)
        self.assertFalse(state.enabled)
        self.assertEqual("requested", state.handle("/team on").action)
        self.assertTrue(state.enabled)
        self.assertEqual("requested", state.handle("生成一份报告").action)
        for text in ("wonderful", "好的", "thanks"):
            with self.subTest(text=text):
                self.assertEqual("none", state.handle(text).action)
        self.assertEqual("none", state.handle("/team off").action)
        self.assertFalse(state.enabled)
        self.assertEqual("none", state.handle("生成一份报告").action)

    def test_env_seeds_enabled(self):
        state = TeamActivationState(enabled=True)
        self.assertEqual("requested", state.handle("生成一份报告").action)


class TeamReminderTest(unittest.TestCase):
    def test_reminder_is_wrapped_and_teaches_the_board_protocol(self):
        decision = resolve_team_activation("并行做三件互不相关的事")
        reminder = team_activation_reminder(decision)
        self.assertIn("[TEAM ORCHESTRATION OPT-IN]", reminder)
        self.assertIn("[/TEAM ORCHESTRATION OPT-IN]", reminder)
        self.assertIn("task_create", reminder)
        self.assertIn("spawn_agent(team_mode=true)", reminder)
        self.assertIn("completed", reminder)


if __name__ == "__main__":
    unittest.main()
