# -*- coding: utf-8 -*-
"""定位 full 模式下输入框那 2 次移动：记录每次移动前后的整屏差异。"""
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
env["PYTHONPATH"] = ROOT
env["PYTHONIOENCODING"] = "utf-8"
env["PYTHONUTF8"] = "1"
env["TERM"] = "xterm-256color"
env["GA_INK_MOUSE"] = "full"

screen = pyte.Screen(COLS, ROWS)
stream = pyte.Stream(screen)
stop = threading.Event()
state = {"prev_row": -1, "prev": None, "events": [], "t0": None}


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


def composer_row(lines):
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
        r = composer_row(lines)
        if state["prev_row"] >= 0 and r != state["prev_row"] and state["prev"]:
            state["events"].append(
                (time.time() - state["t0"], state["prev_row"], r, state["prev"], lines)
            )
        state["prev_row"] = r
        state["prev"] = lines
        time.sleep(0)


threading.Thread(target=reader, daemon=True).start()
time.sleep(30)

p.write("只回复这一个词，不要其他内容：ALPHAONE")
time.sleep(0.8)
p.write("\r")
state["t0"] = time.time()
state["prev_row"] = composer_row(snap())
state["prev"] = snap()
time.sleep(30)
stop.set()
time.sleep(0.3)
try:
    p.terminate(force=True)
except Exception:
    pass

evs = state["events"]
print(f"共 {len(evs)} 次移动\n")
for t, a, b, before, after in evs[:6]:
    print(f"--- [{t:6.2f}s] 输入框下边框 {a} -> {b} ({b - a:+d}) ---")
    for tag, l in (("-", x) for x in before):
        pass
    diff = list(difflib.unified_diff(before, after, lineterm="", n=0))
    added = [l[1:] for l in diff if l.startswith("+") and not l.startswith("+++")]
    removed = [l[1:] for l in diff if l.startswith("-") and not l.startswith("---")]
    print(f"  新增 {len(added)} / 删除 {len(removed)} 行内容")
    for l in added[:8]:
        if l.strip():
            print(f"    + {l[:86]}")
    for l in removed[:8]:
        if l.strip():
            print(f"    - {l[:86]}")
    print()
