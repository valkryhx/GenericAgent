# -*- coding: utf-8 -*-
"""定位 full 模式残余的 2 次移动：每次移动时 dump 底部底栏全部行。"""
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
state = {"prev": None, "events": [], "t0": None, "last": ""}


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


def snap():
    return [row_text(y) for y in range(ROWS)]


def bottom_border(lines):
    for y in range(len(lines) - 1, -1, -1):
        t = lines[y].strip()
        if len(t) >= 8 and set(t) == {"─"}:
            return y
    return -1


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
        lines = snap()
        b = bottom_border(lines)
        prev = state["prev"]
        if prev is not None and b != prev:
            state["events"].append((time.time() - state["t0"], prev, b, list(lines)))
        state["prev"] = b
        time.sleep(0)


threading.Thread(target=reader, daemon=True).start()
time.sleep(30)

state["t0"] = time.time()
state["prev"] = bottom_border(snap())
p.write("请用中文写一篇 40 行的技术说明，每行一句话，逐行编号。直接给正文。")
time.sleep(0.8)
p.write("\r")
time.sleep(50)
stop.set()
time.sleep(0.3)
try:
    p.terminate(force=True)
except Exception:
    pass

evs = state["events"]
print(f"共 {len(evs)} 次移动\n")
for t, a, b, lines in evs[:8]:
    print(f"--- [{t:6.2f}s] 下边框 {a} -> {b} ({b - a:+d}) ---")
    ne = [i for i, l in enumerate(lines) if l.strip()]
    print("  底部 10 个非空行：")
    for i in ne[-10:]:
        print(f"    {i:2d} | {lines[i][:80]}")
    print()
