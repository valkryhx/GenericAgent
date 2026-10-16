import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from genericagent.agents.prompts import build_agent_role_usage_hint  # noqa: E402


class RoleUsageHintTest(unittest.TestCase):
    def test_root_hint_carries_the_lead_team_addendum(self):
        hint = build_agent_role_usage_hint(is_subagent=False)

        self.assertIn("[GA_ROOT_AGENT_USAGE_HINT]", hint)
        self.assertIn("[GA_TEAM_LEAD_HINT]", hint)
        self.assertNotIn("[GA_TEAMMATE_HINT]", hint)
        # CC m0280 coordinator clauses that must survive the port.
        for expected in ("task_create", "team_mode=true", "不要逐一感谢", "新鲜眼睛", "permission laundering"):
            with self.subTest(expected=expected):
                self.assertIn(expected, hint)

    def test_plain_child_hint_has_no_team_clauses(self):
        hint = build_agent_role_usage_hint(is_subagent=True, team_mode=False)

        self.assertIn("[GA_SUBAGENT_USAGE_HINT]", hint)
        self.assertNotIn("[GA_TEAMMATE_HINT]", hint)
        self.assertNotIn("[GA_TEAM_LEAD_HINT]", hint)

    def test_teammate_hint_adds_the_coordination_brief(self):
        hint = build_agent_role_usage_hint(is_subagent=True, team_mode=True)

        self.assertIn("[GA_SUBAGENT_USAGE_HINT]", hint)
        self.assertIn("[GA_TEAMMATE_HINT]", hint)
        # CC m1574/m0357 clauses: invisible text, mandatory send_message, task board, team-lead name.
        for expected in ("send_message", "task_list", "task_update", "team-lead", "裸文本"):
            with self.subTest(expected=expected):
                self.assertIn(expected, hint)

    def test_team_mode_is_ignored_for_the_root(self):
        hint = build_agent_role_usage_hint(is_subagent=False, team_mode=True)

        self.assertIn("[GA_TEAM_LEAD_HINT]", hint)
        self.assertNotIn("[GA_TEAMMATE_HINT]", hint)

    def test_english_variants_keep_the_markers(self):
        root_en = build_agent_role_usage_hint(is_subagent=False, lang_suffix="_en")
        child_en = build_agent_role_usage_hint(is_subagent=True, team_mode=False, lang_suffix="_en")
        mate_en = build_agent_role_usage_hint(is_subagent=True, team_mode=True, lang_suffix="_en")

        self.assertIn("[GA_TEAM_LEAD_HINT]", root_en)
        self.assertIn("coordinator", root_en)
        self.assertIn("[GA_SUBAGENT_USAGE_HINT]", child_en)
        self.assertNotIn("[GA_TEAMMATE_HINT]", child_en)
        self.assertIn("[GA_TEAMMATE_HINT]", mate_en)
        self.assertIn("send_message", mate_en)

    def test_teammate_brief_is_appended_not_replacing_the_subagent_contract(self):
        base = build_agent_role_usage_hint(is_subagent=True, team_mode=False)
        mate = build_agent_role_usage_hint(is_subagent=True, team_mode=True)

        self.assertTrue(mate.startswith(base))
        self.assertGreater(len(mate), len(base))


if __name__ == "__main__":
    unittest.main()
