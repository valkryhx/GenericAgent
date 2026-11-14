# -*- coding: utf-8 -*-
"""反向确认：变异 _workspace_guard.py 的关键守卫，确认对应测试变红。

判定依据（AGENTS.md §8.5）：测试变绿不能证明有效，必须把实现改坏再跑。
每个变异都指定"应当变红"的测试，跑完自动还原文件。
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(r"D:\git_repos\GenericAgent")
TARGET = REPO / "tests" / "probes" / "python" / "_workspace_guard.py"

# (名字, 原片段, 变异片段, 期望变红的测试)
MUTATIONS = [
    (
        "不还原改动（直接跳过 checkout）",
        "            self._restore(to_restore)",
        "            pass  # MUTATED",
        "test_probe_modification_is_reverted",
    ),
    (
        "歇 dirty_before 不剔除 → 会去 checkout 用户的工作",
        "        report.changed = sorted(set(changed) - before_dirty)",
        "        report.changed = sorted(set(changed))  # MUTATED",
        "test_dirty_at_start_is_reported_not_reverted",
    ),
    (
        "suspect 不报告 → 静默放过",
        "        report.suspect = sorted((set(changed) | set(deleted)) & before_dirty)",
        "        report.suspect = []  # MUTATED",
        "test_dirty_at_start_is_reported_not_reverted",
    ),
    (
        "ok 忽略 suspect",
        "            self.changed or self.deleted or self.suspect or self.created or self.git_error",
        "            self.changed or self.deleted or self.created or self.git_error  # MUTATED",
        "test_dirty_at_start_is_reported_not_reverted",
    ),
    (
        "还原前不存补丁",
        "            report.patch_path = self._save_patch(to_restore)",
        "            report.patch_path = ''  # MUTATED",
        "test_patch_is_saved_before_reverting",
    ),
    (
        "__exit__ 不校验（异常路径漏掉）",
        "        self.verify()\n        return False",
        "        return False  # MUTATED",
        "test_verify_runs_even_when_probe_raises",
    ),
    (
        "hy 新文件不报告",
        "        report.created = sorted(p for p in untracked_now if p not in tracked_set)",
        "        report.created = []  # MUTATED",
        "test_new_untracked_file_is_reported_not_deleted",
    ),
    (
        "sha256 恒等 → 检测不到任何改动",
        "            if _sha256(path) != old_hash:",
        "            if False:  # MUTATED",
        "test_probe_modification_is_reverted",
    ),
    (
        "删除不还原",
        "        to_restore = sorted(set(report.changed) | set(report.deleted))",
        "        to_restore = sorted(set(report.changed))  # MUTATED",
        "test_deleted_file_is_restored",
    ),
]


def _write(path: Path, text: str) -> None:
    """写回时强制 LF —— Windows 的 `write_text` 默认会把 \\n 翻成 \\r\\n，
    会让被测文件悄悄变成 CRLF（违反仓库的 LF 铁律）。"""
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)


def main() -> int:
    original = TARGET.read_text(encoding="utf-8")
    results = []
    try:
        for name, old, new, expect_red in MUTATIONS:
            if old not in original:
                results.append((name, "SKIP(anchor missing)", ""))
                continue
            _write(TARGET, original.replace(old, new, 1))
            proc = subprocess.run(
                [sys.executable, "-m", "pytest", "tests/test_workspace_guard.py", "-q", "--tb=no"],
                cwd=str(REPO), capture_output=True, text=True, encoding="utf-8", errors="replace",
            )
            out = (proc.stdout or "") + (proc.stderr or "")
            red_targets = [ln for ln in out.splitlines() if expect_red in ln and "FAILED" in ln]
            status = "RED-OK" if red_targets else ("NO-RED(bad!)" if proc.returncode == 0 else "OTHER-FAIL")
            results.append((name, status, expect_red))
    finally:
        _write(TARGET, original)

    lines = ["=== reverse confirmation ==="]
    bad = 0
    for name, status, tgt in results:
        mark = "✅" if status == "RED-OK" else "❌"
        if status != "RED-OK":
            bad += 1
        lines.append("%s %-46s %-16s -> %s" % (mark, name, status, tgt))
    lines.append("")
    lines.append("ALL MUTATIONS CAUGHT" if bad == 0 else "%d MUTATION(S) NOT CAUGHT" % bad)
    sys.stdout.buffer.write(("\n".join(lines) + "\n").encode("utf-8"))
    return 0 if bad == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
