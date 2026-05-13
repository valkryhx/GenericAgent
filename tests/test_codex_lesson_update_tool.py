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


class CodexLessonUpdateToolTest(unittest.TestCase):
    def test_handler_writes_candidate_lesson_through_tool(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = type("Parent", (), {"task_dir": None, "verbose": False})()
            handler = GenericAgentHandler(parent, last_history=[], cwd=tmp)

            outcome = _exhaust(handler.do_codex_lesson_update({
                "title": "保护用户未提交改动",
                "guidance": "改代码前检查 git status，遇到无关改动只绕开，不重置或覆盖。",
                "category": "git",
                "evidence": ["git_status", "patch"],
                "source_hash": "abc123",
                "confidence": 0.9,
                "state_dir": str(Path(tmp) / "state"),
            }, type("Response", (), {"content": ""})()))

            self.assertEqual(outcome.data["status"], "candidate_recorded")
            self.assertTrue((Path(tmp) / "state" / "candidate_lessons.jsonl").exists())

    def test_handler_rejects_sensitive_candidate(self):
        with tempfile.TemporaryDirectory() as tmp:
            parent = type("Parent", (), {"task_dir": None, "verbose": False})()
            handler = GenericAgentHandler(parent, last_history=[], cwd=tmp)

            outcome = _exhaust(handler.do_codex_lesson_update({
                "title": "坏经验",
                "guidance": r"读取 C:\Users\Administrator\.env 里的 password=abc",
                "category": "security",
                "evidence": ["secret"],
                "source_hash": "abc124",
                "confidence": 0.9,
                "state_dir": str(Path(tmp) / "state"),
            }, type("Response", (), {"content": ""})()))

            self.assertEqual(outcome.data["status"], "rejected")


if __name__ == "__main__":
    unittest.main()
