# -*- coding: utf-8 -*-
"""A/B：主指标是「输入框下移的平滑度」，不是移动次数。

判据（用户可感知的量）：
  1. 单次移动幅度 |Δ| 的分布  —— 幅度越大越像「跳」
  2. 相邻两次移动的最小间隔  —— 间隔太短会看成抖动
  3. 撑满前的总移动距离      —— 决定整段输出里画面走多远

跑法固定：同一 prompt、同一终端 40x120、同样的观测时长。
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
LABEL = sys.argv[1] if len(sys.argv) > 1 else "run"

env = dict(os.environ)
for k in [k for k in env if k.upper().startswith(("CLAUDE", "ANTHROPIC"))]:
    env.pop(k, None)
env["PYTHONPATH"] = ROOT
env["PYTHONIOENCODING"] = "utf-8"
env["PYTHONUTF8"] = "1"
env["TERM"] = "xterm-256color"

screen = pyte.Screen(COLS, ROWS)
stream = pyte.Stream(screen)
_last = {"row": -1}
series = []
lock = threading.Lock()
stop = threading.Event()
state = {"t0": None}


def sample_row():
    try:
        disp = screen.display
    except Exception:
        return _last["row"]
    for y in range(len(disp) - 1, -1, -1):
        if re.fullmatch(r"─{8,}", disp[y].strip()):
            _last["row"] = y
            return y
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
        if state["t0"] is None:
            continue
        r = sample_row()
        with lock:
            series.append((time.time() - state["t0"], r))
        time.sleep(0)


th = threading.Thread(target=reader, daemon=True)
th.start()
time.sleep(30)

PROMPT = "请用中文写一篇技术说明，至少 50 行，每行一句话，逐行编号。直接给正文，不要用工具。"
p.write(PROMPT)
time.sleep(0.8)
p.write("\r")
state["t0"] = time.time()
time.sleep(50)
stop.set()
time.sleep(0.3)
try:
    p.terminate(force=True)
except Exception:
    pass

with lock:
    got = [(t, r) for t, r in series if r >= 0]

print(f"\n===== {LABEL} =====")
if len(got) < 10:
    print(f"FAIL: 采样过少 ({len(got)})")
    sys.exit(1)

rows = [r for _, r in got]
moves = [(got[i][0], got[i + 1][0], rows[i], rows[i + 1], rows[i + 1] - rows[i])
         for i in range(len(rows) - 1) if rows[i] != rows[i + 1]]

print(f"采样 {len(got)}  行号 {min(rows)}~{max(rows)}  移动 {len(moves)} 次")
if not moves:
    print("无移动")
    sys.exit(0)

deltas = [abs(m[4]) for m in moves]
gaps = [round((moves[i + 1][0] - moves[i][0]) * 1000) for i in range(len(moves) - 1)]

print(f"单次幅度分布: {dict(sorted(Counter(deltas).items()))}")
print(f"  最大幅度 {max(deltas)}  平均 {sum(deltas) / len(deltas):.2f}")
print(f"总下移距离 {sum(rows[i + 1] - rows[i] for i in range(len(rows) - 1) if rows[i + 1] > rows[i])} 行")
if gaps:
    print(f"移动间隔(ms): min={min(gaps)} median={int(sorted(gaps)[len(gaps) // 2])} "
          f"<150ms 的占比 {sum(1 for g in gaps if g < 150) / len(gaps):.0%}")
print(f"\n判定指标 A_jerk = 平均幅度 × (1 + 小于150ms间隔占比)")
jerk_ratio = sum(1 for g in gaps if g < 150) / len(gaps) if gaps else 0
print(f"  A_jerk = {sum(deltas) / len(deltas):.2f} × (1 + {jerk_ratio:.2f}) = "
      f"{sum(deltas) / len(deltas) * (1 + jerk_ratio):.2f}")
