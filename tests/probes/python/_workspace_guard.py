# -*- coding: utf-8 -*-
"""探针执行期间的源码护栏（探针用完自动校验工作树）。

背景：2026-11-14 用自然语言跑 ask_user_question 验收探针时，模型在问完
问题后顺手改了 `genericagent/core/pending_input.py`（+87 行）。事后再看
`git status` 干净不代表探针执行期间干净 —— 如果那时已经 commit，污染就
进了历史。

用法（推荐 with，异常路径也能校验）：

    from _workspace_guard import WorkspaceGuard

    def main():
        with WorkspaceGuard(ROOT, capture_dir=OUT_DIR) as guard:
            ...  # 跑真实 GA / LLM
        return 0 if guard.report.ok else 2

设计要点：

1. **只还原开始时干净、跑完变脏的文件。** 开始时就有未提交改动的是用户
   自己的活，擅自 checkout 会毁掉它。这些只报告、不还原。
2. **还原前先存 diff。** 还原不等于消灭证据。
3. **新增未跟踪文件只报告不删除。** 删错的代价比留一个文件高。
4. 用 tracked 文件的 sha256 比对，而不是 `git stash`：直白、无副作用、
   不需要处理"无改动时 stash 返回空串"这类边界。

注意：这里只覆盖 **tracked** 文件。`tests/probes/captures/` 这类产物目录
本来就是未跟踪的，不参与比对。
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path


def _run_git(root: str, *args: str) -> tuple[int, str, str]:
    """跑 git，返回 (returncode, stdout, stderr)，不抛异常。"""
    env = dict(os.environ)
    # 让 git 在中文路径/输出下不乱码；-c 关掉行尾转换导致的假差异
    env["LC_ALL"] = "C.UTF-8"
    try:
        proc = subprocess.run(
            ["git", "-c", "core.autocrlf=false", *args],
            cwd=root, env=env, capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
    except (OSError, ValueError) as exc:  # git 不在 PATH 等
        return 127, "", str(exc)
    return proc.returncode, proc.stdout, proc.stderr


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    try:
        with open(path, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
    except OSError:
        return "<unreadable>"
    return h.hexdigest()


@dataclass
class GuardReport:
    """一次校验的结果。`ok` 为 False 时探针应当返回非 0。

    文件分三类，处理方式不同：

    | 分类 | 含义 | 动作 | 影响 ok |
    | --- | --- | --- | --- |
    | `changed` / `deleted` | 开始时干净、跑完变脏 | 还原 | ✅ 影响 |
    | `suspect` | 开始时已脏、跑完又变了 | **只报告**（分不清是谁改的，checkout 会毁掉用户的活） | ✅ 影响 |
    | `created` | 新增未跟踪 | 只报告（删错的代价更高） | ✅ 影响 |
    | `dirty_before` | 开始时已脏、但没再变 | 不碰 | ❌ 不影响 |
    """

    changed: list[str] = field(default_factory=list)      # 探针改的，已还原
    deleted: list[str] = field(default_factory=list)      # 探针删的，已还原
    suspect: list[str] = field(default_factory=list)      # 已脏又被改，分不清，未还原
    created: list[str] = field(default_factory=list)      # 新增未跟踪，未删除
    dirty_before: list[str] = field(default_factory=list)  # 开始时已脏，未碰
    restored: list[str] = field(default_factory=list)      # 实际做过还原的
    patch_path: str = ""                                   # 还原前的 diff 存档
    git_error: str = ""                                    # git 不可用时的说明

    @property
    def ok(self) -> bool:
        """没有探针引入的改动 = 干净。

        `dirty_before` 不算失败 —— 那是用户自己的未提交工作。
        但 `suspect` 要算：分不清谁改的，就不能说「没问题」。
        """
        return not (
            self.changed or self.deleted or self.suspect or self.created or self.git_error
        )

    def summary(self) -> str:
        if self.git_error:
            return "workspace guard: git unavailable (%s)" % self.git_error
        if self.ok:
            return "workspace guard: clean (%d dirty before, untouched)" % len(
                self.dirty_before
            )
        parts = []
        if self.changed:
            parts.append("reverted %d: %s" % (len(self.changed), ", ".join(self.changed[:8])))
        if self.deleted:
            parts.append("restored %d: %s" % (len(self.deleted), ", ".join(self.deleted[:8])))
        if self.suspect:
            parts.append(
                "SUSPECT (dirty before AND changed now, NOT reverted) %d: %s"
                % (len(self.suspect), ", ".join(self.suspect[:8]))
            )
        if self.created:
            parts.append(
                "NEW untracked (not deleted) %d: %s"
                % (len(self.created), ", ".join(self.created[:8]))
            )
        return "workspace guard: " + "; ".join(parts)


class WorkspaceGuard:
    """快照 → (探针跑) → 校验并还原。

    `capture_dir` 用于存放还原前的 diff；默认落在仓库的
    `tests/probes/captures/`。
    """

    def __init__(self, root: str, capture_dir: str | None = None) -> None:
        self.root = str(Path(root).resolve())
        self.capture_dir = capture_dir or os.path.join(
            self.root, "tests", "probes", "captures"
        )
        self._hashes: dict[str, str] = {}
        self._dirty_before: list[str] = []
        self._tracked: list[str] = []
        self._snapshotted = False
        self.report = GuardReport()

    # -- 快照 ------------------------------------------------------------

    def snapshot(self) -> None:
        """记录所有 tracked 文件的内容哈希 + 当前脏文件集合。"""
        rc, out, err = _run_git(self.root, "ls-files", "-z")
        if rc != 0:
            self.report.git_error = (err or "git ls-files failed").strip()
            return

        self._tracked = [p for p in out.split("\x00") if p]
        self._hashes = {
            rel: _sha256(Path(self.root) / rel) for rel in self._tracked
        }
        self._dirty_before = self._dirty_now()
        self._snapshotted = True

    def _dirty_now(self) -> list[str]:
        rc, out, _ = _run_git(self.root, "status", "--porcelain")
        if rc != 0:
            return []
        paths = []
        for line in out.splitlines():
            if len(line) < 4:
                continue
            rel = line[3:].strip()
            # 重命名时是 "old -> new"，取新路径
            if " -> " in rel:
                rel = rel.split(" -> ", 1)[1]
            paths.append(rel.strip('"'))
        return sorted(paths)

    # -- 校验 ------------------------------------------------------------

    def verify(self) -> GuardReport:
        """比对并还原探针引入的改动。可重复调用。"""
        report = GuardReport()
        if not self._snapshotted:
            report.git_error = self.report.git_error or "snapshot() was not called"
            self.report = report
            return report

        before_dirty = set(self._dirty_before)
        report.dirty_before = sorted(before_dirty)

        changed: list[str] = []
        deleted: list[str] = []
        for rel, old_hash in self._hashes.items():
            path = Path(self.root) / rel
            if not path.exists():
                deleted.append(rel)
                continue
            if _sha256(path) != old_hash:
                changed.append(rel)

        # 新增的未跟踪文件（不含一开始就存在的、也不含 tracked 的删除）
        untracked_now = set(self._dirty_now()) - before_dirty
        tracked_set = set(self._tracked)
        report.created = sorted(p for p in untracked_now if p not in tracked_set)

        # 开始时已脏的文件，探针又碰了 —— 分不清是谁改的，绝不 checkout，
        # 但必须报告（静默剔除等于放过一次真实污染）
        report.changed = sorted(set(changed) - before_dirty)
        report.deleted = sorted(set(deleted) - before_dirty)
        report.suspect = sorted((set(changed) | set(deleted)) & before_dirty)

        to_restore = sorted(set(report.changed) | set(report.deleted))
        if to_restore:
            report.patch_path = self._save_patch(to_restore)
            self._restore(to_restore)
            report.restored = list(to_restore)
        elif report.suspect:
            # 未还原，但仍要留证据，方便人自己判断
            report.patch_path = self._save_patch(report.suspect)

        self.report = report
        return report

    def _save_patch(self, paths: list[str]) -> str:
        """把即将被还原的改动存成 .diff —— 还原不能消灭证据。"""
        os.makedirs(self.capture_dir, exist_ok=True)
        stamp = time.strftime("%Y%m%d-%H%M%S")
        path = os.path.join(self.capture_dir, "workspace-contamination-%s.diff" % stamp)
        rc, out, err = _run_git(self.root, "diff", "--", *paths)
        body = out if rc == 0 else ""
        # 未跟踪/被删文件走 diff 可能为空，补一段说明，避免存一个空文件误导人
        header = (
            "# 探针执行期间出现的源码改动（已还原）\n"
            "# files: %s\n" % ", ".join(paths)
        )
        if not body.strip():
            body = "# (git diff 为空：可能是新增/删除，见上面 files)\n"
        try:
            with open(path, "w", encoding="utf-8", newline="\n") as f:
                f.write(header)
                f.write(body)
        except OSError as exc:
            return "<patch save failed: %s>" % exc
        return path

    def _restore(self, paths: list[str]) -> None:
        """把 tracked 文件恢复成 HEAD 内容。"""
        _run_git(self.root, "checkout", "--", *paths)

    # -- with 支持 -------------------------------------------------------

    def __enter__(self) -> "WorkspaceGuard":
        self.snapshot()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        # 探针抛异常也要校验 —— 异常路径恰恰是最危险的
        self.verify()
        return False
