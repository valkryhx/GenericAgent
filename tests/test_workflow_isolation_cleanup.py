import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from workflow_child_agent import (
    NativeGPTChildAgentRunner,
    _guarded_remove_tree,
    prune_isolated_workspaces,
)
from workflow_models import WorkflowJob


def _job(tmp_root, job_id="agent_iso", run_id="wf_test", **options):
    meta = {"runId": run_id, "options": options} if options else {"runId": run_id}
    return WorkflowJob(job_id=job_id, prompt="do the job", metadata=meta)


class IsolationCleanupTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name) / "child_agents"
        self.env = mock.patch.dict(
            os.environ,
            {
                "GA_WORKFLOW_CHILD_AGENTS_ROOT": str(self.base),
                "GA_WORKFLOW_ISOLATION_TTL_HOURS": "168",
            },
            clear=False,
        )
        self.env.start()
        self.addCleanup(self.env.stop)
        os.environ.pop("GA_WORKFLOW_ISOLATION_KEEP", None)

    def test_unchanged_isolated_workspace_is_removed(self):
        job = _job(self.base, isolation="worktree")
        runner = NativeGPTChildAgentRunner()
        private = Path(runner._child_cwd(job))
        self.assertTrue(private.is_dir())
        runner._finalize_isolated_workspace(job, {})
        self.assertFalse(private.exists())
        # an emptied per-run parent is tidied away too
        self.assertFalse((self.base / "wf_test").exists())

    def test_changed_isolated_workspace_is_kept_by_default(self):
        job = _job(self.base, isolation="worktree")
        runner = NativeGPTChildAgentRunner()
        private = Path(runner._child_cwd(job))
        summary = {"writtenPaths": ["out.py"]}
        runner._finalize_isolated_workspace(job, summary)
        self.assertTrue(private.is_dir())
        self.assertEqual(summary["keptWorkspacePath"], str(private.resolve()))

    def test_policy_never_removes_even_when_changed(self):
        os.environ["GA_WORKFLOW_ISOLATION_KEEP"] = "never"
        job = _job(self.base, isolation="worktree")
        runner = NativeGPTChildAgentRunner()
        private = Path(runner._child_cwd(job))
        summary = {"writtenPaths": ["out.py"]}
        runner._finalize_isolated_workspace(job, summary)
        self.assertFalse(private.exists())
        self.assertNotIn("keptWorkspacePath", summary)

    def test_policy_always_keeps_even_when_unchanged(self):
        os.environ["GA_WORKFLOW_ISOLATION_KEEP"] = "always"
        job = _job(self.base, isolation="worktree")
        runner = NativeGPTChildAgentRunner()
        private = Path(runner._child_cwd(job))
        summary = {}
        runner._finalize_isolated_workspace(job, summary)
        self.assertTrue(private.is_dir())
        self.assertEqual(summary["keptWorkspacePath"], str(private.resolve()))

    def test_unknown_policy_falls_back_to_changed(self):
        os.environ["GA_WORKFLOW_ISOLATION_KEEP"] = "bogus"
        self.assertEqual(NativeGPTChildAgentRunner._isolated_workspace_keep_policy(), "changed")

    def test_non_isolated_shared_workspace_is_not_cleaned(self):
        shared = Path(self._tmp.name) / "shared"
        shared.mkdir()
        job = WorkflowJob(
            job_id="agent_plain",
            prompt="p",
            metadata={"runId": "wf_test", "workspacePath": str(shared)},
        )
        runner = NativeGPTChildAgentRunner()
        summary = {"writtenPaths": ["out.py"]}
        runner._finalize_isolated_workspace(job, summary)
        self.assertTrue(shared.is_dir())
        self.assertNotIn("keptWorkspacePath", summary)

    def test_remove_refuses_path_outside_base(self):
        outside = Path(self._tmp.name) / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("x", encoding="utf-8")
        self.assertFalse(_guarded_remove_tree(outside, self.base))
        self.assertTrue(outside.is_dir())

    def test_remove_refuses_traversal_job_id(self):
        job = _job(self.base, job_id="..", isolation="worktree")
        runner = NativeGPTChildAgentRunner()
        # Build a sibling directory that a naive join could target.
        sibling = self.base / "wf_test"
        sibling.mkdir(parents=True, exist_ok=True)
        (sibling / "keep.txt").write_text("x", encoding="utf-8")
        self.assertFalse(runner._remove_isolated_workspace(job))
        self.assertTrue((sibling / "keep.txt").is_file())

    def test_remove_refuses_symlink(self):
        target = Path(self._tmp.name) / "real"
        target.mkdir()
        (target / "keep.txt").write_text("x", encoding="utf-8")
        link = self.base / "wf_link" / "agent_iso"
        link.parent.mkdir(parents=True, exist_ok=True)
        try:
            os.symlink(target, link, target_is_directory=True)
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable on this platform")
        job = _job(self.base, job_id="agent_iso", run_id="wf_link", isolation="worktree")
        runner = NativeGPTChildAgentRunner()
        self.assertFalse(runner._remove_isolated_workspace(job))
        self.assertTrue((target / "keep.txt").is_file())

    def test_prune_removes_old_keeps_fresh(self):
        old = self.base / "wf_old" / "agent_1"
        fresh = self.base / "wf_new" / "agent_1"
        old.mkdir(parents=True, exist_ok=True)
        fresh.mkdir(parents=True, exist_ok=True)
        now = time.time()
        os.utime(old, (now - 10 * 3600, now - 10 * 3600))
        os.utime(fresh, (now, now))
        removed = prune_isolated_workspaces(ttl_hours=1, base=self.base, now=now)
        self.assertEqual(removed, 1)
        self.assertFalse(old.exists())
        self.assertTrue(fresh.is_dir())

    def test_prune_disabled_when_ttl_zero(self):
        old = self.base / "wf_old" / "agent_1"
        old.mkdir(parents=True, exist_ok=True)
        now = time.time()
        os.utime(old, (now - 10 * 3600, now - 10 * 3600))
        self.assertEqual(prune_isolated_workspaces(ttl_hours=0, base=self.base, now=now), 0)
        self.assertTrue(old.is_dir())

    def test_base_env_override_is_honoured(self):
        job = _job(self.base, isolation="worktree")
        runner = NativeGPTChildAgentRunner()
        cwd = Path(runner._child_cwd(job))
        self.assertTrue(str(cwd).startswith(str(self.base.resolve())))


if __name__ == "__main__":
    unittest.main()
