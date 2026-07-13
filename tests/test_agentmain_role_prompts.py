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


if __name__ == "__main__":
    unittest.main()
