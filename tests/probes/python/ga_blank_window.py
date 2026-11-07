# -*- coding: utf-8 -*-
"""真机验证：统计「屏幕被清空但还没重画」的瞬间出现次数。

判据：一次擦除之后、下一段可见内容之前，快照屏幕。
如果屏幕上可见行数掉到接近 0，说明存在「空窗」= 用户看到的闪。
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

blank_hits = 0
samples = 0
lock = threading.Lock()


def visible_lines():
    return sum(1 for line in screen.display if line.strip())


def reader():
    global blank_hits, samples
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
        # 紧跟在一次 feed 之后采样：这是「擦除已发生、重写尚未到达」的窗口
        with lock:
            n = visible_lines()
            samples += 1
            if n <= 2:
                blank_hits += 1


def poller():
    """独立高频采样：不依赖 TTY 是否有新数据。"""
    global blank_hits, samples
    while not stop.is_set():
        with lock:
            n = visible_lines()
            samples += 1
            if n <= 2:
                blank_hits += 1
        time.sleep(0.002)


th = threading.Thread(target=reader, daemon=True)
th.start()
pl = threading.Thread(target=poller, daemon=True)
pl.start()
time.sleep(30)

p.write("Count from 1 to 30, one number per line.")
time.sleep(1.0)
p.write("\r")
time.sleep(20)

stop.set()
time.sleep(0.3)
try:
    p.terminate(force=True)
except Exception:
    pass

print(f"采样次数        = {samples}")
print(f"近空屏次数(<=2)  = {blank_hits}")
if samples:
    print(f"空窗占比        = {blank_hits / samples * 100:.2f}%")
print(f"COALESCE        = {os.environ.get('GA_UI_WRITE_COALESCE', 'on(default)')}")
