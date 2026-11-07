# -*- coding: utf-8 -*-
"""真机验证：只看「流式已开始之后」的输入框行号是否变化。

之前 .tmp_ga_jitter_probe.py 把启动、空闲折叠、流式、结束都算进去了，
那些阶段切换本来就会改变布局。这个探针只盯流式期。
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
PROMPT = "Count from 1 to 40, one number per line."

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

samples = []
lock = threading.Lock()
stop = threading.Event()


def frame_row():
    """输入框顶边框所在行。"""
    disp = screen.display
    for y in range(len(disp) - 1, -1, -1):
        if re.fullmatch(r"─{8,}", disp[y].strip()):
            return y
    return None


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


th = threading.Thread(target=reader, daemon=True)
th.start()
t0 = time.time()

# 空闲期基线
time.sleep(32)
idle_row = frame_row()
print("空闲期输入框行号:", idle_row, flush=True)

p.write(PROMPT)
time.sleep(1.0)
p.write("\r")

# 等流式开始
stream_start = None
deadline = time.time() + 25
while time.time() < deadline:
    disp = screen.display
    if sum(1 for line in disp if re.fullmatch(r"\s*\d{1,2}\s*", line)) >= 4:
        stream_start = time.time()
        break
    time.sleep(0.2)

print("stream detected:", stream_start is not None, flush=True)

# 主线程定期采样：不依赖 TTY 是否有新输出
rows = []
if stream_start:
    end = time.time() + 25
    while time.time() < end:
        row = frame_row()
        if row is not None:
            rows.append(row)
        time.sleep(0.15)

stop.set()
time.sleep(0.3)
try:
    p.terminate(force=True)
except Exception:
    pass

print("流式期样本数:", len(rows))
if rows:
    uniq = sorted(set(rows))
    flips = sum(1 for a, b in zip(rows, rows[1:]) if a != b)
    print("输入框行号集合:", uniq)
    print("相邻变化次数:", flips, "/", max(0, len(rows) - 1))
    print("结论:", "稳定" if len(uniq) == 1 else "变化 %d 种" % len(uniq))
