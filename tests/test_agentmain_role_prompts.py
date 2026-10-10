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


    def test_system_prompt_is_rendered_from_named_sections(self):
        """One renderer, named sections: the prompt must stay byte-identical.

        pi splits its system prompt into named sections so a caller can tell
        which ones changed; GA's pieces were concatenated inline, which is the
        same text but not diffable.
        """
        with mock.patch.object(agentmain, "load_base_system_prompt", return_value="[base]"):
            with mock.patch.object(agentmain, "build_ga_project_instructions", return_value="[project]"):
                with mock.patch("subagent_notifications.build_subagent_notifications_prompt", return_value="[notify]"):
                    with mock.patch.object(agentmain, "get_global_memory", return_value="[memory]"):
                        with mock.patch.object(agentmain, "build_skill_prompt", return_value="[skills]"):
                            with mock.patch.object(agentmain, "build_agent_role_usage_hint", return_value="[role]"):
                                with mock.patch.object(agentmain, "build_permission_mode_hint", return_value="[perm]"):
                                    sections = agentmain.build_system_prompt_sections()
                                    prompt = agentmain.get_system_prompt()

        self.assertEqual(
            (
                "base",
                "date",
                "project",
                "notifications",
                "memory",
                "skills",
                "role_hint",
                "permission_mode",
            ),
            agentmain.SYSTEM_PROMPT_SECTION_ORDER,
        )
        self.assertEqual(agentmain.SYSTEM_PROMPT_SECTION_ORDER, tuple(sections))
        self.assertEqual("[base]", sections["base"])
        self.assertEqual("[notify]", sections["notifications"])
        self.assertEqual(chr(10) + "[role]" + chr(10), sections["role_hint"])
        self.assertEqual(agentmain.render_system_prompt(sections), prompt)
        self.assertTrue(prompt.startswith("[base]"))
        for earlier, later in (
            ("[project]", "[notify]"),
            ("[notify]", "[memory]"),
            ("[memory]", "[skills]"),
            ("[skills]", "[role]"),
            ("[role]", "[perm]"),
        ):
            with self.subTest(earlier=earlier, later=later):
                self.assertLess(prompt.index(earlier), prompt.index(later))

    def test_diff_reports_only_the_sections_that_changed(self):
        sections = {name: name for name in agentmain.SYSTEM_PROMPT_SECTION_ORDER}
        changed = dict(sections)
        changed["memory"] = "different"

        self.assertEqual(["memory"], agentmain.diff_system_prompt_sections(sections, changed))
        self.assertEqual([], agentmain.diff_system_prompt_sections(sections, dict(sections)))
        self.assertEqual([], agentmain.diff_system_prompt_sections(None, {}))

    def test_note_system_prompt_sections_reports_the_first_turn_as_no_change(self):
        agent = type("Agent", (), {})()

        first = agentmain.note_system_prompt_sections(agent, {"base": "b"})

        self.assertEqual([], first)
        self.assertEqual({"base": "b"}, agent._last_prompt_sections)

    def test_note_system_prompt_sections_names_only_what_changed(self):
        agent = type("Agent", (), {})()
        agentmain.note_system_prompt_sections(agent, {"base": "b", "memory": "m1"})

        changed = agentmain.note_system_prompt_sections(agent, {"base": "b", "memory": "m2"})

        self.assertEqual(["memory"], changed)
        self.assertEqual({"base": "b", "memory": "m2"}, agent._last_prompt_sections)
        self.assertEqual([], agentmain.note_system_prompt_sections(agent, {"base": "b", "memory": "m2"}))

    def test_subagent_skips_the_notifications_section_without_breaking_the_order(self):
        subagent = type("Subagent", (), {"task_dir": str(REPO_ROOT / "temp" / "demo_subagent")})()

        sections = agentmain.build_system_prompt_sections(subagent)

        self.assertEqual("", sections["notifications"])
        self.assertIn("notifications", sections)

    def test_root_hint_carries_shared_workspace_and_cleanup_hygiene(self):
        """Codex's collab prompt requires telling children they are not alone.

        GA's root hint used to be silent about the shared directory, about
        closing finished subagents, and about the limited concurrency slots.
        """
        prompt = self._system_prompt()

        for expected in (
            "共享同一个工作目录",
            "你不是一个人在这个工作区",
            "互不重叠的写入路径",
            "close_agent",
            "并发槽位有限",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, prompt)

    def test_subagent_hint_carries_isolation_and_delivery_semantics(self):
        subagent = type("Subagent", (), {"task_dir": str(REPO_ROOT / "temp" / "demo_subagent")})()

        prompt = self._system_prompt(subagent)

        for expected in (
            "共享同一个工作目录",
            "只改任务分配给你的路径",
            "对你已剥离",
            "回传给父代理",
            "绝不编造数字",
        ):
            with self.subTest(expected=expected):
                self.assertIn(expected, prompt)


if __name__ == "__main__":
    unittest.main()
