"""幂等修复：给 terminalInput.test.ts 的每个期望键对象补上 end: false。

## 为什么改成幂等（2026-11-14）

旧版是 `text.replace("pageUp: false,\n", "pageUp: false,\n      end: false,\n")`，
**每跑一次就多插一行**。它的产物被误提交过两次，导致同一对象里出现 3 个
`end: false`，tsc 报 TS1117（An object literal cannot have multiple
properties with the same name），整个前端类型检查挂掉。

现在：先检测每个 pageUp 后面紧跟的是不是已经是 end: false，
是就跳过。重复运行不再改变文件。
"""
from pathlib import Path
import sys

P = Path(r"D:\git_repos\GenericAgent\frontends\ink\ui\src\terminalInput.test.ts")

lines = P.read_text(encoding="utf-8").split("\n")
out = []
inserted = 0
skipped = 0
for i, line in enumerate(lines):
    out.append(line)
    if line.strip() == "pageUp: false,":
        nxt = lines[i + 1].strip() if i + 1 < len(lines) else ""
        if nxt == "end: false,":
            skipped += 1
            continue
        out.append("      end: false,")
        inserted += 1

text = "\n".join(out)
P.write_text(text, encoding="utf-8", newline="\n")
count = text.count("end: false")
sys.stdout.buffer.write(
    ("inserted=%d skipped=%d total_end=%d\n" % (inserted, skipped, count)).encode()
)
