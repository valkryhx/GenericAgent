# -*- coding: utf-8 -*-
"""最贴近用户感知的测量：pyte 屏幕快照 vs 用户眼里的「画面」。

之前测的是「输入框边框行号」，但 GA 的输入框上方有动态增删的活动行
（MCP 行、目标行、spinner），边框行号会因这些行而变，不等于「整幅画在移动」。

用户说的是「整个画图在 agent 输出时偶尔会轻微上下位移」。
所以要看的是：**屏幕上已有内容的起始行是否发生整体平移**。

做法：每个 write 后取一次屏幕快照，计算「第一个非空行的行号」和
「内容区的整体指纹」。若已有内容的起始行号发生变化 → 整体平移。

另外统计：屏幕上不同内容的行数（= 画面高度），看它是否恒定。
"""
import hashlib
import os
import re
import sys
import threading
import time

import pyte
from winpty import PtyProcess

ROOT = r"D:\git_repos\GenericAgent"
COLS, ROWS = 120, 40

env = dict(os.environ)
for k in [k for k in env if k.upper().startswith(("CLAUDE", "ANTHROPIC"))]:
    env.pop(k, None)
env["PYTHONPATH"] = ROOT
env["PYTHONIOENCODING"] = "utf-8"
env["PYTHONUTF8"] = "1"
env["TERM"] = "xterm-256color"

screen = pyte.Screen(COLS, ROWS)
stream = pyte.Stream(screen)

snaps = []
lock = threading.Lock()
stop = threading.Event()
session = {"t0": None}

p = PtyProcess.spawn(
    ["bash", "-lc", "cd /d/git_repos/GenericAgent && ./ga"],
    dimensions=(ROWS, COLS), env=env, cwd=ROOT,
)


def snapshot():
    disp = screen.display
    lines = [l.rstrip() for l in disp]
    first = -1
    last = -1
    for y, l in enumerate(lines):
        if l.strip():
            if first < 0:
                first = y
            last = y
    fp = hashlib.md5("\n".join(lines).encode("utf-8", "replace")).hexdigest()[:8]
    return first, last, fp


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
        if session["t0"] is None:
            continue
        try:
            s = snapshot()
        except Exception:
            continue
        with lock:
            snaps.append((time.time() - session["t0"], *s))
        time.sleep(0)


th = threading.Thread(target=reader, daemon=True)
th.start()
time.sleep(30)

PROMPT = "请用中文写 3 段话介绍终端渲染，每段 2 句。直接给正文，不要用工具。"
p.write(PROMPT)
time.sleep(0.8)
p.write("\r")
session["t0"] = time.time()
time.sleep(45)
stop.set()
time.sleep(0.3)
try:
    p.terminate(force=True)
except Exception:
    pass

with lock:
    got = [s for s in snaps if s[1] >= 0]

print(f"本轮快照 {len(got)} 个\n")
if not got:
    print("FAIL")
    sys.exit(1)

firsts = [s[1] for s in got]
lasts = [s[2] for s in got]

print("=== 首个非空行（画面上边界）===")
print(f"  范围 {min(firsts)}~{max(firsts)} 出现过的值: {sorted(set(firsts))}")
fch = [(got[i][0], firsts[i], firsts[i + 1]) for i in range(len(firsts) - 1) if firsts[i] != firsts[i + 1]]
print(f"  变化 {len(fch)} 次")

print()
print("=== 末个非空行（画面下边界）===")
print(f"  范围 {min(lasts)}~{max(lasts)} 出现过的值: {sorted(set(lasts))}")
lch = [(got[i][0], lasts[i], lasts[i + 1]) for i in range(len(lasts) - 1) if lasts[i] != lasts[i + 1]]
print(f"  变化 {len(lch)} 次")

print()
print("=== 画面高度（下边界-上边界）===")
heights = [l - f for f, l in zip(firsts, lasts)]
print(f"  范围 {min(heights)}~{max(heights)}")
from collections import Counter
print(f"  分布: {dict(sorted(Counter(heights).items()))}")
hj = [(got[i][0], heights[i], heights[i + 1], heights[i + 1] - heights[i])
      for i in range(len(heights) - 1) if heights[i + 1] != heights[i]]
print(f"  高度变化 {len(hj)} 次，|Δ|>=2 的 {sum(1 for x in hj if abs(x[3]) >= 2)} 次")
for t, a, b, d in hj[:25]:
    print(f"    [{t:6.2f}s] {a:2d} -> {b:2d}  ({d:+d})")

# 关键：上边界是否为空（即画面是否贴顶不滚）
print()
if max(firsts) == 0:
    print("=> 画面上边界始终为 0：内容贴顶，没有整体下移（终端已滚满）")
else:
    print(f"=> 画面上边界最多到第 {max(firsts)} 行：画面确实在屏内移动")

# 整体平移判定：上下边界同向等量变化
print()
both = []
for i in range(len(firsts) - 1):
    df = firsts[i + 1] - firsts[i]
    dl = lasts[i + 1] - lasts[i]
    if df != 0 and df == dl:
        both.append((got[i][0], df))
print(f"=== 整体平移事件（上下边界同向等量移动）: {len(both)} 次 ===")
for t, d in both[:20]:
    print(f"    [{t:6.2f}s] 整体 {d:+d} 行")
