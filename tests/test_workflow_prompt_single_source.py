import unittest
from unittest import mock

import workflow_tool_profiles as wtp
from workflow_child_agent import ROLE_INSTRUCTIONS
from workflow_planner import CODING_AGENT_ROLES, LLMWorkflowPlanner, WorkflowPlanner


def planner_prompt():
    planner = LLMWorkflowPlanner(client=object(), fallback=WorkflowPlanner())
    return planner._planner_prompt("research the topic, then write a report", {}, issues=[], previous_plan=None)


class WorkflowPromptSingleSourceTest(unittest.TestCase):
    """The planner prompt must be generated from the host tables, not copied.

    A hand-copied profile description is a description that goes stale the first
    time someone adds a profile: the host would deny a capability the prompt
    still advertised. Step-Code renders its subagent guidance from its own
    tables for the same reason.
    """

    def test_planner_prompt_is_generated_from_the_profile_table(self):
        with mock.patch.dict(wtp.WORKFLOW_TOOL_PROFILES, {"explore": frozenset({"file_write", "execute"})}):
            prompt = planner_prompt()

        self.assertIn("explore=web_search+web_fetch+file_read", prompt)

    def test_planner_prompt_tracks_a_profile_widening(self):
        with mock.patch.dict(wtp.WORKFLOW_TOOL_PROFILES, {"research": frozenset()}):
            prompt = planner_prompt()

        self.assertIn("research=web_search+web_fetch+file_read+file_write+execute", prompt)

    def test_planner_prompt_derives_the_role_mapping(self):
        with mock.patch.dict(wtp.ROLE_DEFAULT_TOOL_PROFILE, {"scout": "planner"}):
            prompt = planner_prompt()

        self.assertIn("scout=planner", prompt)

    def test_planner_prompt_never_denies_the_unrestricted_profile_its_capabilities(self):
        prompt = planner_prompt()

        self.assertIn("*=web_search+web_fetch+file_read+file_write+execute", prompt)
        self.assertNotIn("orchestration=", prompt)

    def test_planner_prompt_stopped_hand_listing_the_profile_semantics(self):
        prompt = planner_prompt()

        self.assertNotIn("planner=只读+检索", prompt)

    def test_every_canonical_role_has_an_instruction_and_a_known_profile(self):
        self.assertEqual([], sorted(set(CODING_AGENT_ROLES) - set(ROLE_INSTRUCTIONS)))
        self.assertEqual([], sorted(set(ROLE_INSTRUCTIONS) - set(CODING_AGENT_ROLES)))
        for role in sorted(CODING_AGENT_ROLES):
            with self.subTest(role=role):
                profile = wtp.profile_for_role(role)
                self.assertTrue(profile)
                self.assertTrue(wtp.is_known_tool_profile(profile), profile)


if __name__ == "__main__":
    unittest.main()
