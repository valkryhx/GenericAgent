# -*- coding: utf-8 -*-
"""用 pyte 回放 headless 写出的真实字节流，判断 BRAVO 消失是否是真问题。

条款：真实终端在 40 列时会把 80 列写出的长行 reflow 成 2 行。
pyte 支持 reflow 吗？—— pyte 的 Screen 不做 reflow（resize 时截断/保留），
所以这里用**固定宽度**回放：从字节流中找出写入时的宽度，全程按该宽度解释。
更稳妥的做法是按「最终宽度 100」回放，因为长度只有 1 行时不会折行。
"""
import re
import sys

import pyte

path = sys.argv[1] if len(sys.argv) > 1 else ".tmp_bytes.bin"

with open(path, encoding="utf-8", newline="") as f:
    data = f.read()

# 分别用 80 / 100 列回放，看 BRAVO 是否留存
for cols in (80, 100, 120):
    for rows in (24, 40):
        screen = pyte.Screen(cols, rows)
        stream = pyte.Stream(screen)
        try:
            stream.feed(data)
        except Exception as e:
            print(f"cols={cols} rows={rows}: feed 异常 {e}")
            continue
        disp = screen.display
        text = "\n".join(disp)
        bravo = sum(1 for l in disp if "HIST-PROBE-BRAVO" in l)
        alpha = sum(1 for l in disp if "HIST-PROBE-ALPHA" in l)
        print(f"cols={cols:3d} rows={rows:2d}:  BRAVO={bravo}  ALPHA={alpha}  可见行={sum(1 for l in disp if l.strip())}")

print()
# 检查字节里是否有 clearTerminal
ESC = chr(27)
CLEAR = ESC + "[2J" + ESC + "[3J" + ESC + "[H"
print(f"字节流含 clearTerminal: {CLEAR in data}")
print(f"字节流含 ESC[2J: {ESC + '[2J' in data}")
print(f"字节流含 ESC[3J: {ESC + '[3J' in data}")
print(f"字节流含 BRAVO 字面: {'HIST-PROBE-BRAVO' in data}")
