import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from subagent_roles import SubagentRoleRegistry, build_role_task_message  # noqa: E402


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


if __name__ == "__main__":
    unittest.main()
