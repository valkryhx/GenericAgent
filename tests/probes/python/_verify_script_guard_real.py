# -*- coding: utf-8 -*-
"""真仓库验收：脚本式护栏（start/finish）也真的守得住。

对应用户担心的事：探针执行期间模型改源码。这次验的是「脚本式探针」
那条路径（ga_tui_probe / ask_user_question_e2e_probe 用的就是它）。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _workspace_guard import CONTAMINATED_EXIT, finish, start  # noqa: E402

ROOT = r"D:\git_repos\GenericAgent"
TARGET = Path(ROOT) / "genericagent" / "core" / "pending_input.py"


def main() -> int:
    out = []
    original = TARGET.read_text(encoding="utf-8")
    out.append("target: %s (%d bytes)" % (TARGET.name, len(original)))

    try:
        guard = start(ROOT)
        # 模拟模型在探针期间改源码
        with open(TARGET, "w", encoding="utf-8", newline="\n") as f:
            f.write(original + "\n# SCRIPT-STYLE GUARD CHECK\n")

        exited = None
        try:
            finish(guard, code=0)
        except SystemExit as exc:
            exited = exc.code

        out.append("finish() raised SystemExit: %r" % (exited,))
        out.append("CHECK exit code   : %s" % ("PASS" if exited == CONTAMINATED_EXIT else "FAIL"))
        now = TARGET.read_text(encoding="utf-8")
        out.append("CHECK reverted    : %s (%d bytes)" % ("PASS" if now == original else "FAIL", len(now)))
        patch = Path(guard.report.patch_path).read_text(encoding="utf-8") if guard.report.patch_path else ""
        out.append("CHECK patch saved : %s" % ("PASS" if "SCRIPT-STYLE GUARD CHECK" in patch else "FAIL"))

        allpass = exited == CONTAMINATED_EXIT and now == original and "SCRIPT-STYLE GUARD CHECK" in patch
        out.append("")
        out.append("RESULT: " + ("ALL PASS" if allpass else "FAILURE"))
        sys.stdout.buffer.write(("\n".join(out) + "\n").encode("utf-8"))
        return 0 if allpass else 1
    finally:
        if TARGET.read_text(encoding="utf-8") != original:
            with open(TARGET, "w", encoding="utf-8", newline="\n") as f:
                f.write(original)


if __name__ == "__main__":
    sys.exit(main())
