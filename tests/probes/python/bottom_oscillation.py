# -*- coding: utf-8 -*-
"""A/B 专用：只测「撑满终端后的 ±1 来回摆动」。

前一轮已确认：长答案撑满终端后，输入框出现 36->37->36 的来回摆动。
本探针在内容撑满后继续观测 25 秒，只数「到达底部后的行号变化」，
并记录每次变化前后是「帧高变了」还是「帧高没变但内容换了」。

判定：
  - 若底部摆动时帧高不变 → 是内容换行（末行宽度变化 / wrap），不是布局
  - 若底部摆动时帧高也变 → 是布局层问题
"""
import os
import re
import sys
import threading
import time
from collections import Counter

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

series = []
lock = threading.Lock()
stop = threading.Event()
state = {"t0": None}


_last = {"row": -1, "content": 0}


def sample():
    """取一帧屏幕快照。pyte 在宽字符边界上会 IndexError，取不到就沿用上次值。"""
    try:
        disp = screen.display
    except Exception:
        return _last["row"], _last["content"]
    row = -1
    content = 0
    for y in range(len(disp) - 1, -1, -1):
        s = disp[y].strip()
        if row < 0 and re.fullmatch(r"─{8,}", s):
            row = y
    for line in disp:
        s = line.strip()
        if s and not re.fullmatch(r"─{8,}", s):
            content += 1
    _last["row"] = row
    _last["content"] = content
    return row, content


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
        if state["t0"] is None:
            continue
        r, c = sample()
        with lock:
            series.append((time.time() - state["t0"], r, c))
        time.sleep(0)


th = threading.Thread(target=reader, daemon=True)
th.start()
time.sleep(30)

PROMPT = "请用中文写一篇技术说明，至少 60 行，每行一句话，逐行编号。直接给正文，不要用工具。"
p.write(PROMPT)
time.sleep(0.8)
p.write("\r")
state["t0"] = time.time()

# 先等撑满，再继续观测
time.sleep(80)
stop.set()
time.sleep(0.3)
try:
    p.terminate(force=True)
except Exception:
    pass

with lock:
    got = [(t, r, c) for t, r, c in series if r >= 0]

print(f"采样 {len(got)} 个")
if not got:
    print("FAIL")
    sys.exit(1)

rows = [r for _, r, _ in got]
peak = max(rows)
peak_idx = rows.index(peak)
print(f"输入框行号范围 {min(rows)}~{peak}，首次到达最高行号于 {got[peak_idx][0]:.2f}s")

tail = got[peak_idx:]
print(f"撑满后采样 {len(tail)} 个（{tail[-1][0] - tail[0][0]:.1f}s）\n")

moves = []
prev = tail[0][1]
for i in range(1, len(tail)):
    t, r, c = tail[i]
    if r == prev:
        continue
    pc = tail[i - 1][2]
    moves.append((t, prev, r, r - prev, c, c - pc))
    prev = r

print(f"=== 撑满后输入框移动 {len(moves)} 次 ===")
for t, a, b, d, c, dc in moves:
    print(f"  [{t:6.2f}s] {a}->{b} ({d:+d})  内容行 {c} (Δ{dc:+d})")

print()
if moves:
    ds = [m[3] for m in moves]
    dcs = [m[5] for m in moves]
    print(f"行号变化幅度: {dict(sorted(Counter(ds).items()))}")
    print(f"同时内容行变化: {dict(sorted(Counter(dcs).items()))}")
    zero_content = sum(1 for d in dcs if d == 0)
    print()
    if zero_content == len(moves):
        print("=> 每次摆动时内容行数不变：不是布局高度问题")
    elif zero_content > len(moves) / 2:
        print("=> 多数摆动时内容行数不变：主要是渲染/换行细节，不是布局")
    else:
        print("=> 摆动伴随内容行数变化：布局层（帧高随内容变）")
else:
    print("=> 撑满后完全稳定，无摆动（本轮未复现）")
