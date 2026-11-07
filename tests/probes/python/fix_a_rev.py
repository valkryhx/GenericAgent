"""A 方案（反向）：不新增行，而是让「running 但无内容」的占位行不再出现。

依据：内容从 live 移入 <Static> 时总行数不变（Static 增 = live 减）。
真正的 ±1 只来自 keepLivePlaceholder 那 1 行的塌陷：
  D（done 已到、status 仍 running）：live/1  ← 占位行
  E（status 转 idle）：              none/0   ← 占位行没了
两者高度一致，turn 边界就不再变高。

注意：这只在「用户消息本身已在 live 里」时不生效（那时 liveCount>0，
本来就有行）。占位行唯一的作用是「running 且 live 完全为空」时的可见性提示，
那种情况在真实链路里只在极短窗口出现。
"""
import subprocess
import sys
from pathlib import Path

SRC = Path(r"D:\git_repos\GenericAgent\frontends\ink\ui\src\messageViewportPlan.ts")

ORIG = """  if (input.keepLivePlaceholder) {
    // Running with no assistant lines yet: still reserve a short live band so
    // open-turn user / thinking cue stays in the dock (visibility P0-A).
    return { kind: 'live', height: 1 }
  }"""

FIXED = """  if (input.keepLivePlaceholder && !input.hasStaticMessages) {
    // Running with no assistant lines yet and no history: reserve a short live band
    // so the open-turn cue stays in the dock (visibility P0-A).
    //
    // NOT when history already exists: the same 1-row band would collapse to 0 at
    // `status: idle`, shrinking the whole frame by one row — on a full terminal that
    // reads as the picture jumping up one line at every turn end (GA 319 UX parity).
    // The open-turn user message itself already occupies the live viewport there, so
    // nothing is lost by skipping the placeholder.
    return { kind: 'live', height: 1 }
  }"""

action = sys.argv[1] if len(sys.argv) > 1 else "on"
text = SRC.read_text(encoding="utf-8")

if action == "on":
    if FIXED in text:
        print("already on")
        sys.exit(0)
    assert ORIG in text, "找不到 keepLivePlaceholder 分支"
    SRC.write_text(text.replace(ORIG, FIXED), encoding="utf-8", newline="\n")
    print("patched: 占位行仅在无历史时出现")
elif action == "off":
    if FIXED not in text:
        print("already off")
        sys.exit(0)
    SRC.write_text(text.replace(FIXED, ORIG), encoding="utf-8", newline="\n")
    print("reverted")
else:
    print("usage: on|off")
    sys.exit(2)

out = subprocess.run(["git", "diff", "--stat", "--", str(SRC)],
                     cwd=r"D:\git_repos\GenericAgent", capture_output=True, text=True)
print(out.stdout.strip() or "(no diff)")
