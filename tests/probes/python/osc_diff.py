# -*- coding: utf-8 -*-
"""定位撑满终端后的 ±1 摆动：抓抖动前后的整屏文本做 diff。

假设：底栏的「活动行」（spinner `✻ Deciphering (Ns)`）或提示行每秒刷新，
      时而存在时而消失，把底栏撑高/缩矮 1 行 → 输入框 ±1。

做法：在输入框行号发生变化时，dump 变化前后的整屏文本，
      逐行 diff，看是谁多了/少了。
"""
import difflib
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
BOX_RE = re.compile(r"^─{8,}$")
lock = threading.Lock()
stop = threading.Event()
state = {"t0": None, "prev_row": -1, "prev_text": None, "events": []}


def row_text(y):
    buf = screen.buffer.get(y)
    if not buf:
        return ""
    return "".join(
        (buf.get(x).data if buf.get(x) and buf.get(x).data else " ")
        for x in range(COLS)
    ).rstrip()


def full_text():
    try:
        return [row_text(y) for y in range(ROWS)]
    except Exception:
        return None


def composer_row(text):
    if text is None:
        return -1
    for y in range(len(text) - 1, -1, -1):
        if BOX_RE.match(text[y].strip()):
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
        text = full_text()
        if text is None:
            continue
        r = composer_row(text)
        with lock:
            if state["prev_row"] >= 0 and r != state["prev_row"] and state["prev_text"]:
                state["events"].append(
                    (time.time() - state["t0"], state["prev_row"], r, state["prev_text"], text)
                )
            state["prev_row"] = r
            state["prev_text"] = text
        time.sleep(0)


th = threading.Thread(target=reader, daemon=True)
th.start()
time.sleep(30)

PROMPT = "请用中文写一篇技术说明，至少 60 行，每行一句话，逐行编号。直接给正文，不要用工具。"
p.write(PROMPT)
time.sleep(0.8)
p.write("\r")
state["t0"] = time.time()
time.sleep(60)
stop.set()
time.sleep(0.3)
try:
    p.terminate(force=True)
except Exception:
    pass

with lock:
    evs = list(state["events"])

print(f"抓到 {len(evs)} 次行号变化\n")

# 只看幅度为 ±1 的（撑满后的摆动特征）
small = [e for e in evs if abs(e[2] - e[1]) == 1]
print(f"其中幅度 ±1 的 {len(small)} 次，逐条 diff：\n")

shown = 0
for t, a, b, before, after in small:
    if shown >= 6:
        break
    shown += 1
    print(f"--- [{t:6.2f}s] {a} -> {b} ({b - a:+d}) ---")
    diff = list(difflib.unified_diff(before, after, lineterm="", n=1))
    added = [l[1:] for l in diff if l.startswith("+") and not l.startswith("+++")]
    removed = [l[1:] for l in diff if l.startswith("-") and not l.startswith("---")]
    print(f"  新增 {len(added)} 行 / 删除 {len(removed)} 行")
    for l in added[:6]:
        print(f"    + {l[:100]}")
    for l in removed[:6]:
        print(f"    - {l[:100]}")
    print()

# 统计：每次 ±1 时，是否有 spinner / 活动行参与
SPIN = re.compile(r"[✻✽✶✳✢·.*]\s+\S+\s+\(\d+s\)")
with_spin = sum(
    1 for t, a, b, before, after in small
    if any(SPIN.search(l) for l in after) != any(SPIN.search(l) for l in before)
)
print(f"±1 摆动中「spinner 行出现/消失」的次数: {with_spin} / {len(small)}")
if small and with_spin >= len(small) * 0.6:
    print("=> 结论：底栏活动行（spinner）的存在与否是 ±1 摆动的来源")
elif small:
    print("=> spinner 不是主因，看上面 diff 的实际内容")
