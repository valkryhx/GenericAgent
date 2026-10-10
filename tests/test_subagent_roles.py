import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from subagent_roles import (  # noqa: E402
    BUILTIN_ROLES,
    SubagentRoleRegistry,
    build_role_task_message,
    format_role_catalog,
)


class SubagentRolesTest(unittest.TestCase):
    def test_load_json_role_definition(self):
        with tempfile.TemporaryDirectory() as td:
            roles_dir = Path(td) / ".ga" / "subagents"
            roles_dir.mkdir(parents=True)
            (roles_dir / "researcher.json").write_text(
                json.dumps(
                    {
                        "name": "researcher",
                        "description": "Read-only research agent",
                        "when_to_use": "Use for codebase research",
                        "system_prompt": "Only inspect files and summarize evidence.",
                        "permission_profile": "read_only",
                        "allowed_tools": ["file_read", "load_skill"],
                        "model_profile": "inherit",
                        "fork_turns_default": "none",
                        "tools": ["file_read", "code_run"],
                        "allow_delegation": True,
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            role = SubagentRoleRegistry(td).get("researcher")

            self.assertEqual(role.name, "researcher")
            self.assertEqual(role.description, "Read-only research agent")
            self.assertEqual(role.when_to_use, "Use for codebase research")
            self.assertEqual(role.system_prompt, "Only inspect files and summarize evidence.")
            self.assertEqual(role.permission_profile, "read_only")
            self.assertEqual(role.permission_options, {"allowed_tools": ["file_read", "load_skill"]})
            self.assertEqual(role.model_profile, "inherit")
            self.assertEqual(role.fork_turns_default, "none")
            self.assertEqual(role.tools, ("file_read", "code_run"))
            self.assertTrue(role.allow_delegation)
            self.assertEqual(Path(role.source_path), roles_dir / "researcher.json")

    def test_load_markdown_role_definition_with_frontmatter(self):
        with tempfile.TemporaryDirectory() as td:
            roles_dir = Path(td) / ".ga" / "subagents"
            roles_dir.mkdir(parents=True)
            (roles_dir / "auditor.md").write_text(
                "---\n"
                "name: auditor\n"
                "description: Review-only auditor\n"
                "permission_profile: read_only\n"
                "allowed_tools: [file_read, grep]\n"
                "fork_turns_default: 3\n"
                "tools: [file_read, grep]\n"
                "---\n"
                "Check the implementation against the plan.\n",
                encoding="utf-8",
            )

            role = SubagentRoleRegistry(td).get("auditor")

            self.assertEqual(role.name, "auditor")
            self.assertEqual(role.description, "Review-only auditor")
            self.assertEqual(role.permission_profile, "read_only")
            self.assertEqual(role.permission_options, {"allowed_tools": ["file_read", "grep"]})
            self.assertEqual(role.fork_turns_default, "3")
            self.assertEqual(role.tools, ("file_read", "grep"))
            self.assertEqual(role.system_prompt, "Check the implementation against the plan.")

    def test_unknown_role_raises_file_not_found(self):
        with tempfile.TemporaryDirectory() as td:
            registry = SubagentRoleRegistry(td)

            with self.assertRaises(FileNotFoundError):
                registry.get("missing")

    def test_role_message_wraps_role_prompt_without_losing_task(self):
        with tempfile.TemporaryDirectory() as td:
            roles_dir = Path(td) / ".ga" / "subagents"
            roles_dir.mkdir(parents=True)
            (roles_dir / "researcher.json").write_text(
                json.dumps({"name": "researcher", "system_prompt": "Inspect only."}),
                encoding="utf-8",
            )
            role = SubagentRoleRegistry(td).get("researcher")

            message = build_role_task_message(role, "Find relevant tests.")

            self.assertIn("[GA_SUBAGENT_ROLE]", message)
            self.assertIn("name: researcher", message)
            self.assertIn("Inspect only.", message)
            self.assertTrue(message.rstrip().endswith("Find relevant tests."))

    def test_builtin_roles_exist_without_any_configuration(self):
        """A fresh install must have the read-only defaults pi/Step-Code ship.

        GA shipped no roles at all, so the model had to hand-build the boundary
        out of permission_profile + allowed_tools on every spawn -- or, more
        often, spawn a full-access child for read-only work.
        """
        with tempfile.TemporaryDirectory() as td:
            registry = SubagentRoleRegistry(td)

            names = [role.name for role in registry.list_roles()]

            self.assertEqual(["explore", "plan", "review"], names)
            for name in names:
                with self.subTest(role=name):
                    role = registry.get(name)
                    self.assertEqual("read_only", role.permission_profile)
                    self.assertTrue(role.system_prompt)
                    self.assertTrue(role.when_to_use)

    def test_project_role_overrides_the_builtin_of_the_same_name(self):
        with tempfile.TemporaryDirectory() as td:
            roles_dir = Path(td) / ".ga" / "subagents"
            roles_dir.mkdir(parents=True)
            (roles_dir / "explore.json").write_text(
                json.dumps({"name": "explore", "system_prompt": "Project-specific exploration rules."}),
                encoding="utf-8",
            )

            role = SubagentRoleRegistry(td).get("explore")
            names = [item.name for item in SubagentRoleRegistry(td).list_roles()]

            self.assertEqual("Project-specific exploration rules.", role.system_prompt)
            self.assertEqual(1, names.count("explore"))
            self.assertIn("plan", names)
            self.assertIn("review", names)

    def test_role_catalog_annotates_each_role_with_its_capability(self):
        """Listing bare names is not enough: the caller picks before seeing a catalog."""

        catalog = format_role_catalog(BUILTIN_ROLES)

        self.assertIn("explore (read-only", catalog)
        self.assertIn("plan (read-only", catalog)
        self.assertIn("review (read-only", catalog)

    def test_role_catalog_marks_a_writable_project_role_as_writable(self):
        """A description must never hide the capability note."""
        from types import SimpleNamespace

        catalog = format_role_catalog(
            [SimpleNamespace(name="builder", permission_profile="workspace_write", description="edits files")]
        )

        self.assertIn("builder (can write", catalog)
        self.assertIn("edits files", catalog)

    def test_role_catalog_falls_back_to_the_permission_profile(self):
        """A role file without a description still gets a truthful capability note."""
        from types import SimpleNamespace

        catalog = format_role_catalog([SimpleNamespace(name="auditor", permission_profile="read_only")])

        self.assertIn("auditor (read-only)", catalog)


if __name__ == "__main__":
    unittest.main()
