import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from subagent_capabilities import (  # noqa: E402
    INTERNAL_SENTINEL_TOOLS,
    ORCHESTRATION_TOOLS,
    build_subagent_capability_profile,
    filter_tool_schema_for_capabilities,
)
from ga import GenericAgentHandler  # noqa: E402


class SubagentCapabilityProfileTest(unittest.TestCase):
    def test_root_profile_keeps_orchestration_tools(self):
        profile = build_subagent_capability_profile(is_subagent=False)

        self.assertTrue(profile.allow_delegation)
        self.assertTrue(ORCHESTRATION_TOOLS.issubset(profile.allowed_tools))
        self.assertTrue(profile.allows("spawn_agent"))
        self.assertTrue(profile.allows("no_tool"))

    def test_child_profile_removes_orchestration_tools_by_default(self):
        profile = build_subagent_capability_profile(
            is_subagent=True,
            role_tools=["file_read", "code_run", "spawn_agent"],
        )

        self.assertFalse(profile.allow_delegation)
        self.assertTrue(profile.allows("file_read"))
        self.assertTrue(profile.allows("code_run"))
        self.assertFalse(profile.allows("spawn_agent"))
        self.assertTrue(INTERNAL_SENTINEL_TOOLS.issubset(profile.allowed_tools))

    def test_child_can_explicitly_opt_into_delegation(self):
        profile = build_subagent_capability_profile(
            is_subagent=True,
            role_tools=["file_read"],
            allow_delegation=True,
        )

        self.assertTrue(profile.allow_delegation)
        self.assertTrue(profile.allows("spawn_agent"))

    def test_unknown_tools_are_denied(self):
        profile = build_subagent_capability_profile(is_subagent=True, role_tools=["file_read"])

        self.assertFalse(profile.allows("made_up_tool"))
        self.assertFalse(profile.allows(""))
        self.assertTrue(profile.allows("no_tool"))

    def test_schema_filter_does_not_mutate_input(self):
        schema = [
            {"type": "function", "function": {"name": "file_read"}},
            {"type": "function", "function": {"name": "spawn_agent"}},
            {"type": "function", "function": {"name": "close_agent"}},
        ]
        profile = build_subagent_capability_profile(is_subagent=True, role_tools=["file_read"])

        filtered = filter_tool_schema_for_capabilities(schema, profile)

        self.assertEqual([item["function"]["name"] for item in filtered], ["file_read"])
        self.assertEqual(
            [item["function"]["name"] for item in schema],
            ["file_read", "spawn_agent", "close_agent"],
        )

    def test_handler_hard_denies_a_hallucinated_orchestration_call(self):
        handler = GenericAgentHandler(SimpleNamespace(task_dir=""), cwd=".")
        handler.capability_profile = build_subagent_capability_profile(
            is_subagent=True,
            role_tools=["file_read"],
        )
        generator = handler.dispatch("spawn_agent", {}, SimpleNamespace())

        with self.assertRaises(StopIteration) as stopped:
            while True:
                next(generator)

        result = stopped.exception.value
        self.assertEqual(result.data["status"], "error")
        self.assertEqual(result.data["capability"]["reason"], "tool_not_granted")


if __name__ == "__main__":
    unittest.main()
