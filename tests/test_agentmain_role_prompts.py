import sys
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


import agentmain  # noqa: E402


class AgentMainRolePromptsTest(unittest.TestCase):
    def _system_prompt(self, agent=None):
        with mock.patch.object(agentmain, "build_ga_project_instructions", return_value="\n[GA_PROJECT_INSTRUCTIONS]\nproject fake\n[/GA_PROJECT_INSTRUCTIONS]\n"):
            with mock.patch.object(agentmain, "get_global_memory", return_value="\n[Memory fake]\n"):
                with mock.patch.object(agentmain, "build_skill_prompt", return_value="\n[Skills fake]\n"):
                    return agentmain.get_system_prompt(agent)

    def test_root_agent_prompt_gets_root_usage_hint_by_default(self):
        prompt = self._system_prompt()

        self.assertIn("[GA_ROOT_AGENT_USAGE_HINT]", prompt)
        self.assertNotIn("[GA_SUBAGENT_USAGE_HINT]", prompt)
        self.assertTrue("critical path" in prompt or "关键路径" in prompt)

    def test_subagent_prompt_gets_subagent_usage_hint(self):
        subagent = type("Subagent", (), {"task_dir": str(REPO_ROOT / "temp" / "demo_subagent")})()

        prompt = self._system_prompt(subagent)

        self.assertIn("[GA_SUBAGENT_USAGE_HINT]", prompt)
        self.assertNotIn("[GA_ROOT_AGENT_USAGE_HINT]", prompt)
        self.assertTrue("final answer contract" in prompt or "最终结果契约" in prompt)
    def test_project_instructions_are_injected_before_memory_and_skills(self):
        prompt = self._system_prompt()

        self.assertIn("[GA_PROJECT_INSTRUCTIONS]", prompt)
        self.assertLess(prompt.index("[GA_PROJECT_INSTRUCTIONS]"), prompt.index("[Memory fake]"))
        self.assertLess(prompt.index("[Memory fake]"), prompt.index("[Skills fake]"))

    def test_root_agent_prompt_injects_subagent_notifications_before_memory(self):
        with mock.patch("subagent_notifications.build_subagent_notifications_prompt", return_value="\n[GA_SUBAGENT_NOTIFICATIONS]\nfake\n"):
            prompt = self._system_prompt()

        self.assertIn("[GA_SUBAGENT_NOTIFICATIONS]", prompt)
        self.assertLess(prompt.index("[GA_SUBAGENT_NOTIFICATIONS]"), prompt.index("[Memory fake]"))

    def test_subagent_prompt_does_not_consume_parent_notifications(self):
        subagent = type("Subagent", (), {"task_dir": str(REPO_ROOT / "temp" / "demo_subagent")})()
        with mock.patch("subagent_notifications.build_subagent_notifications_prompt", return_value="\n[GA_SUBAGENT_NOTIFICATIONS]\nfake\n") as mocked:
            prompt = self._system_prompt(subagent)

        self.assertNotIn("[GA_SUBAGENT_NOTIFICATIONS]", prompt)
        mocked.assert_not_called()


if __name__ == "__main__":
    unittest.main()
