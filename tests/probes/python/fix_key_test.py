"""给 terminalInput.test.ts 的每个期望键对象补上 end: false。"""
from pathlib import Path

p = Path(r"D:\git_repos\GenericAgent\frontends\ink\ui\src\terminalInput.test.ts")
text = p.read_text(encoding="utf-8")

before = text.count("      pageUp: false,\n")
assert before > 0, "找不到 pageUp 字段"

# 在每个 pageUp: false 之后插入 end: false（保持缩进一致）
text = text.replace(
    "      pageUp: false,\n",
    "      pageUp: false,\n      end: false,\n",
)

p.write_text(text, encoding="utf-8", newline="\n")
print(f"patched {before} 处")
