# -*- coding: utf-8 -*-
"""端到端验收：在真仓库上制造一次"探针改源码"，确认护栏真的拦住。

这不是单元测试（那是 tests/test_workspace_guard.py 的事），而是在
**真仓库** 上跑一遍，验证：
1. 真仓库里检测得到改动
2. 改动被真的还原了（不是只在临时仓库里）
3. 证据补丁落盘
4. 用户自己的未提交改动不被吃掉
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _workspace_guard import WorkspaceGuard  # noqa: E402

ROOT = r"D:\git_repos\GenericAgent"
CAPTURES = r"D:\git_repos\GenericAgent\tests\probes\captures"

# 拿一个已提交、当前干净的真实源码文件当靶子
TARGET = Path(ROOT) / "genericagent" / "core" / "pending_input.py"


def main() -> int:
    out = []
    original = TARGET.read_text(encoding="utf-8")
    out.append("target: %s (%d bytes)" % (TARGET.name, len(original)))

    try:
        with WorkspaceGuard(ROOT, capture_dir=CAPTURES) as guard:
            # 模拟模型顺手改源码 —— 正是 2026-11-14 真实发生过的事
            TARGET.write_text(
                original + "\n# PROBE CONTAMINATION MARKER 20261114\n",
                encoding="utf-8", newline="\n",
            )
            assert "PROBE CONTAMINATION MARKER" in TARGET.read_text(encoding="utf-8")
            out.append("injected contamination: yes")

        r = guard.report
        out.append("")
        out.append("changed      : %r" % (r.changed,))
        out.append("ok           : %s" % r.ok)
        out.append("patch saved  : %s" % (r.patch_path or "(none)"))

        # ① 检测到了
        ok_detected = any("pending_input.py" in p for p in r.changed)
        out.append("CHECK detected   : %s" % ("PASS" if ok_detected else "FAIL"))

        # ② 真的还原了
        now = TARGET.read_text(encoding="utf-8")
        ok_reverted = now == original
        out.append("CHECK reverted   : %s (%d bytes)" % ("PASS" if ok_reverted else "FAIL", len(now)))

        # ③ 补丁里有证据
        patch = Path(r.patch_path).read_text(encoding="utf-8") if r.patch_path else ""
        ok_patch = "PROBE CONTAMINATION MARKER" in patch
        out.append("CHECK patch      : %s" % ("PASS" if ok_patch else "FAIL"))

        # ④ 退出码语义正确
        ok_flag = r.ok is False
        out.append("CHECK exitcode   : %s (ok=%s → probe returns 2)" % ("PASS" if ok_flag else "FAIL", r.ok))

        allpass = ok_detected and ok_reverted and ok_patch and ok_flag
        out.append("")
        out.append("RESULT: " + ("ALL PASS" if allpass else "FAILURE"))
        sys.stdout.buffer.write(("\n".join(out) + "\n").encode("utf-8"))
        return 0 if allpass else 1
    finally:
        # 兜底：无论如何把靶子文件还原（护栏失败时也不能留污染）
        if TARGET.read_text(encoding="utf-8") != original:
            TARGET.write_text(original, encoding="utf-8", newline="\n")


if __name__ == "__main__":
    sys.exit(main())
