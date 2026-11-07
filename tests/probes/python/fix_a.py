"""A 方案：让 turn 结束时视口高度不塌到 0，保持 1 行恒高。

根因（已由 .tmp_layer_attribution.ts 确证）：
  D 阶段（done 已到、status 仍 running）：plan = live/1，视口 1 行
  E 阶段（status 转 idle）：           plan = none/0，视口 0 行
  → 整帧高度 6 -> 5，终端已撑满时就是一次整体 ±1。

修法：idle 且有历史时仍保留 1 行视口，使 turn 边界高度不变。
注意这**不是** Phase-1 的「always messageRows」过修（那会留一大片空白），
只是 1 行。
"""
import subprocess
import sys
from pathlib import Path

SRC = Path(r"D:\git_repos\GenericAgent\frontends\ink\ui\src\messageViewportPlan.ts")

ORIG = """  // Idle with scrollback history: no empty live spacer — stick composer to Static tail.
  return { kind: 'none' }"""

FIXED = """  // Idle with scrollback history. Keep ONE row instead of collapsing to zero:
  // a `live/1` -> `none/0` transition at turn end shrinks the whole frame by one
  // row, which on a full terminal reads as the picture jumping up one line every
  // turn. One row is enough to hold the height steady without the large empty gap
  // that the old "always messageRows" overfix produced.
  return { kind: 'live', height: 1 }"""

action = sys.argv[1] if len(sys.argv) > 1 else "on"
text = SRC.read_text(encoding="utf-8")

if action == "on":
    if FIXED in text:
        print("already on")
        sys.exit(0)
    assert ORIG in text, "找不到 original none 分支"
    SRC.write_text(text.replace(ORIG, FIXED), encoding="utf-8", newline="\n")
    print("patched: idle 保持 1 行视口")
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
