import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from ga import GenericAgentHandler  # noqa: E402


def _exhaust(gen):
    try:
        while True:
            next(gen)
    except StopIteration as e:
        return e.value


class LoadSkillToolTest(unittest.TestCase):
    def test_tool_schema_exposes_load_skill(self):
        schema_path = REPO_ROOT / "assets" / "tools_schema.json"
        tools = json.loads(schema_path.read_text(encoding="utf-8"))
        tool = next(item for item in tools if item["function"]["name"] == "load_skill")
        props = tool["function"]["parameters"]["properties"]

        self.assertIn("skill", props)
        self.assertIn("args", props)

    def test_handler_loads_skill_and_tracks_active_allowed_tools(self):
        with tempfile.TemporaryDirectory() as tmp:
            skills_root = Path(tmp) / "skills"
            skill_dir = skills_root / "demo"
            skill_dir.mkdir(parents=True)
            (skill_dir / "SKILL.md").write_text(
                "---\n"
                "name: demo\n"
                "description: Demo skill\n"
                "allowed-tools: file_read, code_run\n"
                "---\n"
                "Read ${GA_SKILL_DIR}. Args: $ARGUMENTS",
                encoding="utf-8",
            )

            parent = type("Parent", (), {"task_dir": None, "verbose": False})()
            handler = GenericAgentHandler(parent, last_history=[], cwd=tmp)

            outcome = _exhaust(
                handler.do_load_skill(
                    {
                        "skill": "demo",
                        "args": "hello",
                        "search_roots": [str(skills_root)],
                    },
                    type("Response", (), {"content": ""})(),
                )
            )

        self.assertEqual(outcome.data["status"], "success")
        self.assertEqual(outcome.data["name"], "demo")
        self.assertEqual(outcome.data["allowed_tools"], ["file_read", "code_run"])
        self.assertIn("Base directory for this skill:", outcome.data["content"])
        self.assertIn("Args: hello", outcome.data["content"])
        self.assertEqual(handler.working["active_skill"], "demo")


if __name__ == "__main__":
    unittest.main()
