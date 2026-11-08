"""A subagent must not lose ordinary tools because of how it was spawned.

Real incident: four research subagents were spawned and all four came back blocked,
reporting that their tool set was only ``task_*`` / ``send_message`` / ``team_status``
-- no search, no code_run. The parent had not named any tools.

The cause was a whitelist where it should have been a blacklist. ``role_tools`` built
the allowed set, so "no preference expressed" (None, or the empty tuple every builtin
role and every role file without ``tools`` produces) became "grant nothing". With
``team_mode`` on, TEAMMATE_TOOLS was then added on top of that empty set -- yielding a
tool set that looked populated but could not do any work at all.

This pins the contract: capability is opt-out. A child gets every tool the process has,
minus orchestration, minus whatever the caller explicitly denies. A model that forgets
to pass anything cannot silently strip the child's ability to work.

CC agrees (m0779 ``cI``): ``tools`` undefined -> ``hasWildcard: true`` -> keep
everything after the default filter; only an explicit list narrows.
"""

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from genericagent.agents.capabilities import (  # noqa: E402
    ORCHESTRATION_TOOLS,
    TEAMMATE_TOOLS,
    build_subagent_capability_profile,
)

# A representative slice of a real schema: ordinary work tools plus orchestration.
SCHEMA = [
    "file_read", "file_write", "file_patch", "file_glob", "file_grep",
    "code_run", "web_scan", "web_fetch", "load_skill", "ask_user",
    "spawn_agent", "wait_agent", "list_agents", "send_message",
    "task_create", "task_get", "task_update", "task_list", "team_status",
    "workflow", "no_tool",
]
ORDINARY = {"file_read", "file_write", "file_patch", "file_glob", "file_grep",
            "code_run", "web_scan", "web_fetch", "load_skill", "ask_user"}


def profile(**kwargs):
    return build_subagent_capability_profile(available_tools=SCHEMA, **kwargs)


class DefaultSurfaceTest(unittest.TestCase):
    def test_an_unconstrained_child_gets_every_ordinary_tool(self):
        p = profile(is_subagent=True)

        self.assertTrue(ORDINARY.issubset(p.allowed_tools))

    def test_an_unconstrained_child_cannot_orchestrate(self):
        p = profile(is_subagent=True)

        self.assertFalse(p.allowed_tools & {"spawn_agent", "wait_agent", "list_agents", "workflow"})

    def test_the_empty_role_list_is_not_a_denial(self):
        """The exact shape that broke the four research subagents."""
        p = profile(is_subagent=True, role_tools=[])

        self.assertTrue(ORDINARY.issubset(p.allowed_tools), "builtin role stripped the child")

    def test_the_empty_role_list_with_team_mode_keeps_ordinary_tools(self):
        """The exact shape from the incident: a populated-looking but useless tool set."""
        p = profile(is_subagent=True, role_tools=[], team_mode=True)

        self.assertTrue(ORDINARY.issubset(p.allowed_tools))
        self.assertTrue(TEAMMATE_TOOLS.issubset(p.allowed_tools))
        # The failure signature: TOOLS minus TEAMMATE_TOOLS being empty.
        self.assertTrue(p.allowed_tools - TEAMMATE_TOOLS - ORCHESTRATION_TOOLS - {"no_tool"})

    def test_a_non_empty_role_list_no_longer_narrows(self):
        """``role_tools`` is not a whitelist; it is accepted for compatibility only."""
        p = profile(is_subagent=True, role_tools=["file_read"])

        self.assertTrue(ORDINARY.issubset(p.allowed_tools))

    def test_team_mode_only_adds_collaboration_tools(self):
        without = profile(is_subagent=True)
        with_team = profile(is_subagent=True, team_mode=True)

        self.assertEqual(with_team.allowed_tools - without.allowed_tools, TEAMMATE_TOOLS)

    def test_the_main_agent_keeps_orchestration(self):
        p = profile(is_subagent=False)

        self.assertTrue({"spawn_agent", "wait_agent", "workflow"}.issubset(p.allowed_tools))

    def test_allow_delegation_restores_orchestration_for_a_child(self):
        p = profile(is_subagent=True, allow_delegation=True)

        self.assertIn("spawn_agent", p.allowed_tools)


class ExplicitDenialStillWorksTest(unittest.TestCase):
    """Opt-out is the supported way to narrow a child; it must keep working exactly."""

    def test_denied_tools_subtract(self):
        p = profile(is_subagent=True, denied_tools=["file_write", "file_patch"])

        self.assertNotIn("file_write", p.allowed_tools)
        self.assertNotIn("file_patch", p.allowed_tools)
        self.assertIn("file_read", p.allowed_tools)

    def test_denying_an_orchestration_tool_is_harmless(self):
        p = profile(is_subagent=True, denied_tools=["spawn_agent"])

        self.assertNotIn("spawn_agent", p.allowed_tools)
        self.assertIn("file_read", p.allowed_tools)

    def test_no_tool_can_never_be_denied(self):
        """It is how a child delivers its final answer; denying it wedges the child."""
        p = profile(is_subagent=True, denied_tools=["no_tool"])

        self.assertIn("no_tool", p.allowed_tools)

    def test_an_empty_schema_does_not_grant_phantom_tools(self):
        """A lightweight test backend passes no schema; do not invent capabilities."""
        p = build_subagent_capability_profile(is_subagent=True, available_tools=[])

        self.assertFalse(p.allowed_tools - ORCHESTRATION_TOOLS - TEAMMATE_TOOLS - {"no_tool"})


class BuiltinRolesDoNotRestrictTest(unittest.TestCase):
    def test_builtin_roles_carry_no_permission_profile_or_tools(self):
        from genericagent.agents.roles import BUILTIN_ROLES

        for role in BUILTIN_ROLES:
            with self.subTest(role=role.name):
                self.assertIsNone(role.permission_profile)
                self.assertEqual((), role.tools)
                # They must still be real prompts, or they are pointless.
                self.assertTrue(role.system_prompt)
                self.assertTrue(role.when_to_use)

    def test_a_builtin_role_spawn_gets_a_full_surface(self):
        """End to end through the registry shape the handler builds."""
        from genericagent.agents.roles import BUILTIN_ROLES

        explore = next(r for r in BUILTIN_ROLES if r.name == "explore")
        role_tools = list(explore.tools)  # what do_spawn_agent passes
        p = profile(is_subagent=True, role_tools=role_tools)

        self.assertTrue(ORDINARY.issubset(p.allowed_tools))


if __name__ == "__main__":
    unittest.main()
