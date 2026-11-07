# -*- coding: utf-8 -*-
"""用户视角：流式期间「屏幕可见行数」与「输入框行号」的高频时间序列。

用来判断「轻微抖动」到底是：
  A. 可见行数在中途下降（= 内容消失了又出现 → 闪）
  B. 输入框行号微动（= 位置抖）
  C. 两者都不是（在应用层看不到，可能纯属终端渲染）
"""
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
p = PtyProcess.spawn(
    ["bash", "-lc", "cd /d/git_repos/GenericAgent && ./ga"],
    dimensions=(ROWS, COLS), env=env, cwd=ROOT,
)
stop = threading.Event()
lock = threading.Lock()


def snapshot():
    disp = screen.display
    visible = sum(1 for line in disp if line.strip())
    frame_row = None
    for y in range(len(disp) - 1, -1, -1):
        if re.fullmatch(r"─{8,}", disp[y].strip()):
            frame_row = y
            break
    # 内容行（非边框、非提示）
    content = sum(
        1 for line in disp
        if line.strip() and not re.fullmatch(r"─{8,}", line.strip())
    )
    return visible, frame_row, content


def reader():
    while not stop.is_set():
        try:
            raw = p.read(65536)
        except Exception:
            break
        if not raw:
            time.sleep(0.005)
            continue
        try:
            stream.feed(raw)
        except Exception:
            pass


th = threading.Thread(target=reader, daemon=True)
th.start()
time.sleep(30)

p.write("Count from 1 to 30, one number per line.")
time.sleep(1.0)
p.write("\r")

series = []
t_start = time.time()
while time.time() - t_start < 18:
    with lock:
        v, f, c = snapshot()
    series.append((time.time() - t_start, v, f, c))
    time.sleep(0.01)

stop.set()
time.sleep(0.3)
try:
    p.terminate(force=True)
except Exception:
    pass

print(f"采样数 = {len(series)}")
if series:
    vis = [s[1] for s in series]
    frames = [s[2] for s in series if s[2] is not None]
    contents = [s[3] for s in series]

    def report(name, arr):
        if not arr:
            print(f"{name}: 无数据")
            return
        print(f"{name}: min={min(arr)} max={max(arr)} 变化 {sum(1 for a,b in zip(arr,arr[1:]) if a!=b)} 次")

    report("可见行数", vis)
    report("输入框行号", frames)
    report("内容行数", contents)

    # 检测「下降」事件（可见行数减少 = 内容消失）
    drops = [(series[i][0], vis[i], vis[i+1]) for i in range(len(vis)-1) if vis[i+1] < vis[i]]
    print(f"\n可见行数下降事件: {len(drops)} 次")
    for t, a, b in drops[:10]:
        print(f"  [{t:5.2f}s] {a} -> {b}")
