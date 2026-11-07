# -*- coding: utf-8 -*-
"""在 turn 切换的瞬间 dump 屏幕，看清到底发生了什么。"""
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


def dump(label):
    print(f"\n===== {label} =====", flush=True)
    for i, line in enumerate(screen.display):
        if line.strip():
            print(f"{i:2}| {line}", flush=True)


th = threading.Thread(target=reader, daemon=True)
th.start()

time.sleep(30)
dump("boot")

p.write("Say OK")
time.sleep(0.8)
p.write("\r")
time.sleep(6)
dump("turn1 done")

p.write("Say OK again")
time.sleep(0.8)
p.write("\r")

for t in (0.4, 0.9, 1.4, 2.0, 3.0, 4.5):
    time.sleep(0.5 if t == 0.4 else 0.5)
    dump(f"turn2 +{t:.1f}s")

stop.set()
time.sleep(0.3)
try:
    p.terminate(force=True)
except Exception:
    pass
