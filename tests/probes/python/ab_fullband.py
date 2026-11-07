"""临时补丁：让 live 区返回满高（= room），输入框从一开始就贴底。

仅用于 A/B 验证「满高钉底」是否真的能消除画面下移。
验证完必须立刻还原。
"""
import sys
from pathlib import Path

SRC = Path(r"D:\git_repos\GenericAgent\frontends\ink\ui\src\messageViewportPlan.ts")

ORIG = """  if (liveCount > 0) {
    const height = Math.min(liveCount, maxLive, room)
    return { kind: 'live', height }"""

TEST = """  if (liveCount > 0) {
    const height = room  // GA_AB_FULLBAND
    return { kind: 'live', height }"""

action = sys.argv[1] if len(sys.argv) > 1 else "on"
text = SRC.read_text(encoding="utf-8")

if action == "on":
    if "GA_AB_FULLBAND" in text:
        print("already on")
        sys.exit(0)
    assert ORIG in text, "找不到原始分支"
    SRC.write_text(text.replace(ORIG, TEST), encoding="utf-8", newline="\n")
    print("patched: live 区返回满高")
elif action == "off":
    if "GA_AB_FULLBAND" not in text:
        print("already off")
        sys.exit(0)
    SRC.write_text(text.replace(TEST, ORIG), encoding="utf-8", newline="\n")
    print("reverted")
else:
    print("usage: on|off")
    sys.exit(2)
