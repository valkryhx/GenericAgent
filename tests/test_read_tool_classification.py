"""The built-in search tools must be classified as reads, not "unknown".

`file_glob` / `file_grep` were added on 2026-11-08 (see
``docs/20261108-GA内置工具集补齐设计.md``). Both the handler methods
(``do_file_glob`` / ``do_file_grep``) and the schema entries exist and work, but
neither tool was added to the static classification tables that decide *what a
tool is*:

  * ``genericagent/core/permissions/policy.py::_STATIC_READ_TOOLS``
  * ``genericagent/workflow/permissions.py::ALLOWED_READ_ONLY_STATIC_TOOLS``
  * ``genericagent/workflow/tool_profiles.py::READ_TOOL_NAMES``

The consequence is not "the tool is missing" but something more confusing: under
the default ``full_access`` mode nothing looks wrong, and the moment a read-only
mode or a workflow profile is in play, a pure read operation is treated as an
unrecognised *mutating* tool:

    classify_tool("file_glob") -> ("mutating", "static_unknown")
    workflow _evaluate_read_only -> deny / read_only_static_unknown

From the model's side that surfaces as "this tool is not available/recognised",
which is what the user reported. Failure to classify a read tool is a security
default applied in the wrong direction: it blocks reading, which is the safe
operation, while the reason string says the *name* is unknown.

The rule this file pins: **every static read tool is registered as a read in
every table that classifies tools.** A tool missing from one table but present
in another is the exact shape of this bug.
"""
from __future__ import annotations

import unittest

from genericagent.agents.capabilities import build_subagent_capability_profile
from genericagent.core.permissions.policy import build_permission_mode_policy, classify_tool
from genericagent.core import app as app_module
from genericagent.workflow import permissions as workflow_permissions
from genericagent.workflow import tool_profiles


# The search/read surface a release ships. If a new read tool is added, it belongs
# here AND in every classification table -- that is the point of the test.
READ_TOOLS = ("file_read", "file_glob", "file_grep")


class CorePermissionClassificationTest(unittest.TestCase):
    def test_read_tools_classify_as_read(self):
        for tool in READ_TOOLS:
            with self.subTest(tool=tool):
                kind, reason = classify_tool(tool)
                self.assertEqual(
                    "read",
                    kind,
                    f"{tool} classified as {kind!r} ({reason}); a read-only mode will "
                    f"refuse it and report the name as unrecognised",
                )

    def test_read_tools_are_not_reported_as_unknown(self):
        for tool in READ_TOOLS:
            with self.subTest(tool=tool):
                _, reason = classify_tool(tool)
                self.assertNotIn("unknown", reason, f"{tool} still classified as unknown: {reason}")

    def test_writing_tools_still_classify_as_mutating(self):
        # The fix must not widen the read class to everything that starts with "file".
        for tool in ("file_write", "file_patch", "code_run"):
            with self.subTest(tool=tool):
                self.assertEqual("mutating", classify_tool(tool)[0])

    def test_read_only_mode_allows_the_search_tools(self):
        # End-to-end consequence: the reported symptom is a read-only session
        # refusing a directory listing.
        read_only = build_permission_mode_policy("read_only")
        for tool in READ_TOOLS:
            with self.subTest(tool=tool):
                decision = read_only.evaluate(tool)
                self.assertEqual(
                    "allow",
                    decision.action,
                    f"read-only mode refused {tool} with reason={decision.reason}",
                )


class WorkflowReadOnlyClassificationTest(unittest.TestCase):
    def test_search_tools_are_in_the_read_only_allowlist(self):
        for tool in READ_TOOLS:
            with self.subTest(tool=tool):
                self.assertIn(
                    tool,
                    workflow_permissions.ALLOWED_READ_ONLY_STATIC_TOOLS,
                    f"{tool} missing from ALLOWED_READ_ONLY_STATIC_TOOLS",
                )

    def test_read_only_profile_allows_a_directory_listing(self):
        from genericagent.workflow.permissions import READ_ONLY, ToolPermissionPolicy

        policy = ToolPermissionPolicy(profile=READ_ONLY)
        for tool in READ_TOOLS:
            with self.subTest(tool=tool):
                decision = policy.evaluate(tool)
                self.assertEqual(
                    "allow",
                    decision.action,
                    f"read-only workflow profile refused {tool} with reason={decision.reason}",
                )


class CapabilityClassTest(unittest.TestCase):
    """Workflow profiles are expressed as capability classes, not tool names.

    ``tool_capabilities`` maps a tool name to the classes it belongs to. A read
    tool that maps to *no* class is silently permitted everywhere (nothing denies
    it) while also being invisible to coverage reporting -- the worst of both.
    """

    def test_search_tools_carry_the_file_read_capability(self):
        for tool in READ_TOOLS:
            with self.subTest(tool=tool):
                capabilities = tool_profiles.tool_capabilities(tool)
                self.assertIn(
                    "file_read",
                    capabilities,
                    f"{tool} belongs to no capability class; coverage reporting cannot see it",
                )

    def test_capability_coverage_can_see_the_search_tools(self):
        coverage = tool_profiles.capability_coverage(list(READ_TOOLS))
        self.assertEqual(
            sorted(READ_TOOLS),
            coverage.get("file_read"),
            "file_read capability coverage does not list the search tools",
        )

    def test_a_read_profile_still_denies_writes(self):
        # Registering them as reads must not accidentally grant the write class.
        for tool in READ_TOOLS:
            with self.subTest(tool=tool):
                self.assertNotIn("file_write", tool_profiles.tool_capabilities(tool))
                self.assertFalse(tool_profiles.declares_writes({"requiredTools": [tool]}))


class SubagentSurfaceTest(unittest.TestCase):
    def test_a_subagent_keeps_the_search_tools(self):
        # Subagents are the case the user actually hit. Their surface is derived
        # from the concrete schema, so a missing classification would not remove
        # the tool -- but a permission profile would then refuse to let it run.
        app_module.load_tool_schema("")
        names = [
            item.get("function", {}).get("name")
            for item in app_module.TOOLS_SCHEMA
            if isinstance(item, dict)
        ]
        profile = build_subagent_capability_profile(is_subagent=True, available_tools=names)
        for tool in READ_TOOLS:
            with self.subTest(tool=tool):
                self.assertTrue(profile.allows(tool), f"subagent capability profile denies {tool}")


if __name__ == "__main__":
    unittest.main()