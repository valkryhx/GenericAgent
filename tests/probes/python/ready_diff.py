# -*- coding: utf-8 -*-
"""dump Ready. 消失前后的整屏，看究竟是哪一行在动。"""
import difflib
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
env.pop("GA_INK_MOUSE", None)
env["PYTHONPATH"] = ROOT
env["PYTHONIOENCODING"] = "utf-8"
env["PYTHONUTF8"] = "1"
env["TERM"] = "xterm-256color"

screen = pyte.Screen(COLS, ROWS)
stream = pyte.Stream(screen)
stop = threading.Event()
state = {"prev_row": None, "prev": None, "events": [], "t0": None}


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


def composer(lines):
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
            time.sleep(0.002)
            continue
        try:
            stream.feed(raw)
        except Exception:
            pass
        lines = snap()
        c = composer(lines)
        if state["prev_row"] is not None and c != state["prev_row"] and state["prev"]:
            state["events"].append((time.time() - state["t0"], state["prev_row"], c, state["prev"], lines))
        state["prev_row"] = c
        state["prev"] = lines
        time.sleep(0)


state["t0"] = time.time()
threading.Thread(target=reader, daemon=True).start()
time.sleep(20)
stop.set()
time.sleep(0.3)
try:
    p.terminate(force=True)
except Exception:
    pass

print(f"启动阶段捕获 {len(state['events'])} 次移动\n")
for t, a, b, before, after in state["events"]:
    print(f"===== [{t:5.2f}s] 输入框 {a} -> {b} ({b - a:+d}) =====")
    diff = list(difflib.unified_diff(before, after, lineterm="", n=0))
    added = [l[1:] for l in diff if l.startswith("+") and not l.startswith("+++")]
    removed = [l[1:] for l in diff if l.startswith("-") and not l.startswith("---")]
    print(f"  新增 {len(added)} 行 / 删除 {len(removed)} 行")
    for l in removed[:6]:
        if l.strip():
            print(f"    - {l[:84]}")
    for l in added[:6]:
        if l.strip():
            print(f"    + {l[:84]}")
    print("  移动后的整屏非空行：")
    for i, l in enumerate(after):
        if l.strip():
            print(f"    {i:2d} | {l[:84]}")
    print()
