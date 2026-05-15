import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]


class SkillsRuntimeTest(unittest.TestCase):
    def test_discovers_claude_and_codex_skills_from_home(self):
        from skills_runtime import discover_skills

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            claude_skill = home / ".claude" / "skills" / "review"
            codex_skill = home / ".codex" / "skills" / "imagegen"
            claude_skill.mkdir(parents=True)
            codex_skill.mkdir(parents=True)
            (claude_skill / "SKILL.md").write_text(
                "---\n"
                "name: review\n"
                "description: Review code for regressions\n"
                "---\n"
                "# Review\n",
                encoding="utf-8",
            )
            (codex_skill / "SKILL.md").write_text(
                "---\n"
                "name: imagegen\n"
                "description: Generate bitmap assets\n"
                "---\n"
                "# Imagegen\n",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {"USERPROFILE": str(home), "HOME": str(home)}, clear=False):
                skills = discover_skills()

        self.assertEqual([s.name for s in skills], ["review", "imagegen"])
        self.assertTrue(str(skills[0].path).endswith(os.path.join(".claude", "skills", "review", "SKILL.md")))
        self.assertTrue(str(skills[1].path).endswith(os.path.join(".codex", "skills", "imagegen", "SKILL.md")))

    def test_discovers_nested_codex_system_skills(self):
        from skills_runtime import discover_skills

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / ".codex" / "skills"
            skill_dir = root / ".system" / "openai-docs"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\n"
                "name: openai-docs\n"
                "description: Use official OpenAI docs\n"
                "---\n"
                "# OpenAI Docs\n",
                encoding="utf-8",
            )

            skills = discover_skills([root])

        self.assertEqual([s.name for s in skills], ["openai-docs"])

    def test_deduplicates_skills_by_name_using_root_order(self):
        from skills_runtime import discover_skills

        with tempfile.TemporaryDirectory() as tmp:
            first_root = Path(tmp) / "first"
            second_root = Path(tmp) / "second"
            (first_root / "dup").mkdir(parents=True)
            (second_root / "dup").mkdir(parents=True)
            (second_root / "unique").mkdir(parents=True)
            (first_root / "dup" / "SKILL.md").write_text(
                "---\nname: dup\ndescription: First wins\n---\n# First\n",
                encoding="utf-8",
            )
            (second_root / "dup" / "SKILL.md").write_text(
                "---\nname: dup\ndescription: Second loses\n---\n# Second\n",
                encoding="utf-8",
            )
            (second_root / "unique" / "SKILL.md").write_text(
                "---\nname: unique\ndescription: Unique skill\n---\n# Unique\n",
                encoding="utf-8",
            )

            skills = discover_skills([first_root, second_root])

        self.assertEqual([s.name for s in skills], ["dup", "unique"])
        self.assertEqual(skills[0].description, "First wins")

    def test_discovery_cache_reuses_parsed_skills_until_files_change(self):
        import skills_runtime

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "skills"
            skill_dir = root / "demo"
            skill_dir.mkdir(parents=True)
            skill_file = skill_dir / "SKILL.md"
            skill_file.write_text(
                "---\nname: demo\ndescription: Cached\n---\n# Demo\n",
                encoding="utf-8",
            )

            skills_runtime.clear_skill_cache()
            with patch.object(
                skills_runtime,
                "parse_skill_markdown",
                wraps=skills_runtime.parse_skill_markdown,
            ) as parser:
                self.assertEqual([s.name for s in skills_runtime.discover_skills([root])], ["demo"])
                self.assertEqual([s.name for s in skills_runtime.discover_skills([root])], ["demo"])
                self.assertEqual(parser.call_count, 1)

                skill_file.write_text(
                    "---\nname: demo\ndescription: Changed text\n---\n# Demo\n",
                    encoding="utf-8",
                )
                self.assertEqual(skills_runtime.discover_skills([root])[0].description, "Changed text")
                self.assertEqual(parser.call_count, 2)

    def test_skill_listing_is_budgeted_and_uses_when_to_use(self):
        from skills_runtime import SkillSpec, format_skill_listing

        skills = [
            SkillSpec(
                name="xlsx",
                description="Work with spreadsheet files",
                source="codex",
                root=Path("C:/skills/xlsx"),
                path=Path("C:/skills/xlsx/SKILL.md"),
                body="body",
                when_to_use="Use whenever a spreadsheet is the primary input.",
            ),
            SkillSpec(
                name="long",
                description="x" * 500,
                source="claude",
                root=Path("C:/skills/long"),
                path=Path("C:/skills/long/SKILL.md"),
                body="body",
            ),
        ]

        listing = format_skill_listing(skills, char_budget=220)

        self.assertIn("- xlsx: Work with spreadsheet files - Use whenever", listing)
        self.assertLessEqual(len(listing), 220)
        self.assertIn("long", listing)

    def test_load_skill_content_adds_base_directory_and_args(self):
        from skills_runtime import load_skill_content

        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = Path(tmp) / "demo"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text(
                "---\nname: demo\ndescription: Demo skill\n---\n"
                "Use ${CLAUDE_SKILL_DIR} and ${GA_SKILL_DIR}. Args: $ARGUMENTS",
                encoding="utf-8",
            )

            loaded = load_skill_content("demo", search_roots=[Path(tmp)], args="abc")

        self.assertEqual(loaded["name"], "demo")
        self.assertIn("Base directory for this skill:", loaded["content"])
        self.assertIn("Args: abc", loaded["content"])
        self.assertNotIn("$ARGUMENTS", loaded["content"])


if __name__ == "__main__":
    unittest.main()
