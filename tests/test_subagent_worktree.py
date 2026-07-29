import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from subagent_worktree import create_subagent_worktree, remove_subagent_worktree, summarize_subagent_worktree  # noqa: E402


class SubagentWorktreeTest(unittest.TestCase):
    def test_create_subagent_worktree_uses_detached_git_worktree(self):
        with tempfile.TemporaryDirectory() as td:
            calls = []
            repo = Path(td) / "repo"
            repo.mkdir()
            (repo / ".git").mkdir()
            base = Path(td) / "worktrees"

            def fake_runner(cmd, **kwargs):
                calls.append((cmd, kwargs))
                Path(cmd[-2]).mkdir(parents=True)
                return type("Result", (), {"returncode": 0, "stdout": "ok", "stderr": ""})()

            result = create_subagent_worktree(repo, base, "run_000001", runner=fake_runner)

            self.assertEqual(result["status"], "created")
            self.assertEqual(Path(result["path"]), base / "run_000001")
            cmd, kwargs = calls[0]
            self.assertEqual(cmd[:4], ["git", "-C", str(repo), "worktree"])
            self.assertIn("--detach", cmd)
            self.assertEqual(cmd[-1], "HEAD")
            self.assertTrue(kwargs["capture_output"])

    def test_create_subagent_worktree_requires_git_repository(self):
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                create_subagent_worktree(Path(td) / "not_repo", Path(td) / "worktrees", "run_000001")

    def test_summarize_subagent_worktree_captures_status_and_diff(self):
        with tempfile.TemporaryDirectory() as td:
            calls = []
            worktree = Path(td) / "worktree"
            worktree.mkdir()

            def fake_runner(cmd, **kwargs):
                calls.append((cmd, kwargs))
                if cmd[-1] == "--short":
                    return type("Result", (), {"returncode": 0, "stdout": " M agentmain.py\n?? notes.md\n", "stderr": ""})()
                return type("Result", (), {"returncode": 0, "stdout": "diff --git a/agentmain.py b/agentmain.py\n+changed\n", "stderr": ""})()

            summary = summarize_subagent_worktree(worktree, runner=fake_runner)

            self.assertEqual(summary["status"], "dirty")
            self.assertEqual(summary["changed_files"], ["agentmain.py", "notes.md"])
            self.assertIn("+changed", summary["diff"])
            self.assertEqual(calls[0][0], ["git", "-C", str(worktree), "status", "--short"])
            self.assertEqual(calls[1][0], ["git", "-C", str(worktree), "diff", "--stat"])
            self.assertTrue(calls[0][1]["capture_output"])

    def test_remove_subagent_worktree_runs_git_remove_and_deletes_leftover_path(self):
        with tempfile.TemporaryDirectory() as td:
            calls = []
            repo = Path(td) / "repo"
            repo.mkdir()
            worktree = Path(td) / "worktree"
            worktree.mkdir()
            (worktree / "leftover.txt").write_text("leftover", encoding="utf-8")

            def fake_runner(cmd, **kwargs):
                calls.append((cmd, kwargs))
                return type("Result", (), {"returncode": 0, "stdout": "", "stderr": ""})()

            result = remove_subagent_worktree(repo, worktree, runner=fake_runner)

            self.assertEqual(result["status"], "removed")
            self.assertFalse(worktree.exists())
            self.assertEqual(calls[0][0], ["git", "-C", str(repo), "worktree", "remove", "--force", str(worktree)])


if __name__ == "__main__":
    unittest.main()
