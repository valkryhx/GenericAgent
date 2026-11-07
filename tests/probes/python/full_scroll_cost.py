# -*- coding: utf-8 -*-
"""full 模式的代价：失去终端原生 scrollback。

跑一个明显超出一屏的回答（60+ 行），看：
  1. 输入框是否仍然固定不动
  2. 超出屏幕的历史是否还在（备用屏没有 scrollback，只能靠 GA 自己滚）
  3. 页上面最老的内容是否被挤掉
"""
import os
import threading
import time

import pyte
from winpty import PtyProcess

ROOT = r"D:\git_repos\GenericAgent"
COLS, ROWS = 110, 40

env = dict(os.environ)
for k in [k for k in env if k.upper().startswith(("CLAUDE", "ANTHROPIC"))]:
    env.pop(k, None)
env["PYTHONPATH"] = ROOT
env["PYTHONIOENCODING"] = "utf-8"
env["PYTHONUTF8"] = "1"
env["TERM"] = "xterm-256color"
env["GA_INK_MOUSE"] = "full"

screen = pyte.Screen(COLS, ROWS)
stream = pyte.Stream(screen)
stop = threading.Event()
_last = {"row": -1}
series = []


def row_text(y):
    try:
        buf = screen.buffer.get(y)
        if not buf:
            return ""
        return "".join(
            (buf.get(x).data if buf.get(x) and buf.get(x).data else " ")
            for x in range(COLS)
        ).rstrip()
    except Exception:
        return ""


def composer_row():
    try:
        for y in range(ROWS - 1, -1, -1):
            t = row_text(y).strip()
            if len(t) >= 8 and set(t) == {"─"}:
                _last["row"] = y
                return y
    except Exception:
        pass
    return _last["row"]


p = PtyProcess.spawn(
    ["bash", "-lc", "cd /d/git_repos/GenericAgent && ./ga"],
    dimensions=(ROWS, COLS), env=env, cwd=ROOT,
)


def reader():
    while not stop.is_set():
        try:
            raw = p.read(65536)
        except Exception:
            break
        if not raw:
            time.sleep(0.004)
            continue
        try:
            stream.feed(raw)
        except Exception:
            pass
        series.append((time.time(), composer_row()))
        time.sleep(0)


threading.Thread(target=reader, daemon=True).start()
time.sleep(30)

p.write("请用中文写一篇 60 行的技术说明，每行一句话，逐行编号 1 到 60。直接给正文。")
time.sleep(0.8)
p.write("\r")
time.sleep(60)

rows = [r for _, r in series if r >= 0]
moves = [(i, rows[i], rows[i + 1]) for i in range(len(rows) - 1) if rows[i] != rows[i + 1]]
lines = [row_text(y) for y in range(ROWS)]
text = "\n".join(lines)

# 找编号 1 和 60 是否都在屏上（= 是否能看到完整回答）
import re
nums = set()
for l in lines:
    for m in re.finditer(r"(?:^|\s)(\d{1,2})[.、)]", l):
        nums.add(int(m.group(1)))

stop.set()
time.sleep(0.3)
try:
    p.terminate(force=True)
except Exception:
    pass

print("=== full 模式：超一屏回答 ===")
print(f"  输入框行号范围 {min(rows) if rows else '-'}~{max(rows) if rows else '-'}，移动 {len(moves)} 次")
print(f"  屏幕可见编号 {sorted(n for n in nums if 1 <= n <= 60)[:20]}")
print(f"  是否看到第 1 行: {1 in nums}   是否看到第 60 行: {60 in nums}")
print("\n  屏幕内容：")
for i, l in enumerate(lines):
    if l.strip():
        print(f"    {i:2d} | {l[:86]}")
