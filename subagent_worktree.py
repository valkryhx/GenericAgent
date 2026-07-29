from __future__ import annotations

import shutil
import subprocess
from pathlib import Path


_CREATE_NO_WINDOW = 0x08000000


def _run_git(cmd, *, runner=None, timeout=120):
    kwargs = {"capture_output": True, "text": True, "timeout": timeout}
    try:
        import os
        if os.name == "nt":
            kwargs["creationflags"] = _CREATE_NO_WINDOW
    except Exception:
        pass
    return (runner or subprocess.run)(cmd, **kwargs)


def create_subagent_worktree(repo_dir, base_dir, run_id, *, runner=None):
    repo_dir = Path(repo_dir)
    if not (repo_dir / ".git").exists():
        raise ValueError(f"not a git repository: {repo_dir}")
    base_dir = Path(base_dir)
    worktree_path = base_dir / str(run_id)
    worktree_path.parent.mkdir(parents=True, exist_ok=True)
    cmd = ["git", "-C", str(repo_dir), "worktree", "add", "--detach", str(worktree_path), "HEAD"]
    result = _run_git(cmd, runner=runner)
    if getattr(result, "returncode", 1) != 0:
        stderr = (getattr(result, "stderr", "") or "").strip()
        raise RuntimeError(stderr or "git worktree add failed")
    return {"status": "created", "path": str(worktree_path), "run_id": str(run_id)}


def summarize_subagent_worktree(worktree_path, *, runner=None, diff_args=None, max_diff_chars=20000):
    worktree_path = Path(worktree_path)
    if not worktree_path.exists():
        return {"status": "missing", "worktree_path": str(worktree_path), "changed_files": [], "status_text": "", "diff": ""}
    status_result = _run_git(["git", "-C", str(worktree_path), "status", "--short"], runner=runner)
    if getattr(status_result, "returncode", 1) != 0:
        stderr = (getattr(status_result, "stderr", "") or "").strip()
        return {"status": "error", "worktree_path": str(worktree_path), "changed_files": [], "status_text": "", "diff": "", "error": stderr or "git status failed"}
    status_text = getattr(status_result, "stdout", "") or ""
    changed_files = []
    for line in status_text.splitlines():
        text = line[3:].strip() if len(line) >= 3 else line.strip()
        if " -> " in text:
            text = text.split(" -> ", 1)[1].strip()
        if text:
            changed_files.append(text)
    diff_cmd = ["git", "-C", str(worktree_path), "diff", *(diff_args or ["--stat"])]
    diff_result = _run_git(diff_cmd, runner=runner)
    diff_text = getattr(diff_result, "stdout", "") or ""
    if len(diff_text) > max_diff_chars:
        half = max_diff_chars // 2
        diff_text = f"{diff_text[:half]}\n\n[omitted long worktree diff]\n\n{diff_text[-half:]}"
    return {
        "status": "dirty" if changed_files else "clean",
        "worktree_path": str(worktree_path),
        "changed_files": changed_files,
        "status_text": status_text,
        "diff": diff_text,
    }


def remove_subagent_worktree(repo_dir, worktree_path, *, runner=None):
    repo_dir = Path(repo_dir)
    worktree_path = Path(worktree_path)
    result = _run_git(["git", "-C", str(repo_dir), "worktree", "remove", "--force", str(worktree_path)], runner=runner)
    status = "removed"
    error = None
    if getattr(result, "returncode", 1) != 0:
        status = "remove_failed"
        error = (getattr(result, "stderr", "") or "").strip() or "git worktree remove failed"
    if worktree_path.exists():
        shutil.rmtree(worktree_path, ignore_errors=True)
    payload = {"status": status, "worktree_path": str(worktree_path)}
    if error:
        payload["error"] = error
    return payload
