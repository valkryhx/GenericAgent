# -*- coding: utf-8 -*-
"""A/B 对照：同一探针、同一终端尺寸，测「每轮输入框移动次数」。

判据只看 turn 内（从发送到该轮结束）的移动次数，不含 boot。
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


def frame_row():
    disp = screen.display
    for y in range(len(disp) - 1, -1, -1):
        if re.fullmatch(r"─{8,}", disp[y].strip()):
            return y
    return None


th = threading.Thread(target=reader, daemon=True)
th.start()

time.sleep(30)

def run_turn(label):
    """发一轮，采样 8 秒，返回 (移动次数, 行号序列)。"""
    p.write("Say OK")
    time.sleep(0.6)
    p.write("\r")
    rows = []
    end = time.time() + 8
    while time.time() < end:
        r = frame_row()
        if r is not None:
            rows.append(r)
        time.sleep(0.1)
    moves = sum(1 for a, b in zip(rows, rows[1:]) if a != b)
    uniq = sorted(set(rows))
    print(f"{label}: 采样={len(rows)} 移动={moves} 行号={uniq}", flush=True)
    time.sleep(1.5)
    return moves


m1 = run_turn("turn1")
m2 = run_turn("turn2")
m3 = run_turn("turn3")

stop.set()
time.sleep(0.3)
try:
    p.terminate(force=True)
except Exception:
    pass

print(f"\nRESULT rows={ROWS} moves_per_turn={[m1, m2, m3]}")
