# -*- coding: utf-8 -*-
"""基线版：找出输入框到底在什么时候移动。

之前只测流式期，结论是"不抖"。但用户说抖，说明抖发生在别的时刻。
这个探针全程记录输入框行号 + 触发时刻，找出位移的**事件源**。
"""
import os
import re
import sys
import threading
import time

import pyte
from winpty import PtyProcess

ROOT = r"D:\git_repos\GenericAgent"
COLS = 120
ROWS = int(sys.argv[1]) if len(sys.argv) > 1 else 40

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
    dimensions=(ROWS, COLS),
    env=env,
    cwd=ROOT,
)

stop = threading.Event()


def reader():
    while not stop.is_set():
        try:
            raw = p.read(65536)
        except Exception:
            break
        if not raw:
            time.sleep(0.01)
            continue
        try:
            stream.feed(raw)
        except Exception:
            pass


def frame_row():
    disp = screen.display
    for y in range(len(disp) - 1, -1, -1):
        if re.fullmatch(r"─{8,}", disp[y].strip()):
            return y
    return None


th = threading.Thread(target=reader, daemon=True)
th.start()
t0 = time.time()

timeline = []   # (elapsed, event, row)


def mark(event):
    row = frame_row()
    timeline.append((time.time() - t0, event, row))
    print(f"[{time.time()-t0:6.2f}s] {event:<24} row={row}", flush=True)


# 1) 启动
time.sleep(30)
mark("boot done")

# 2) 第一轮：短问答
p.write("Say OK")
time.sleep(0.8)
mark("typed turn1")
p.write("\r")
mark("sent turn1")
for _ in range(30):
    time.sleep(0.5)
    mark("turn1 streaming")
time.sleep(3)
mark("turn1 done")

# 3) 第二轮：直接回车再问
p.write("Say OK again")
time.sleep(0.8)
mark("typed turn2")
p.write("\r")
mark("sent turn2")
for _ in range(20):
    time.sleep(0.5)
    mark("turn2 streaming")

stop.set()
time.sleep(0.3)
try:
    p.terminate(force=True)
except Exception:
    pass

print("\n===== 时间线（只显示行号变化点）=====")
prev = None
changes = 0
for el, ev, row in timeline:
    if row != prev:
        print(f"[{el:6.2f}s] {ev:<24} row={row}  <-- 变化")
        changes += 1
        prev = row
print(f"\n总采样 {len(timeline)}，行号变化 {changes} 次")
