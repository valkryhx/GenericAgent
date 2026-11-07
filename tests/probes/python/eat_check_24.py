# -*- coding: utf-8 -*-
"""真机 24 行终端验证：spacer 改动会不会吃掉上一轮内容。

resizeReflow 的 headless 模型（24 行）报 BRAVO=0，真机 40 行不复现。
差别就在终端高度，所以必须在**真机 24 行**上验证 —— 这是决定 spacer
方案能不能用的唯一判据。
"""
import os
import sys
import threading
import time

import pyte
from winpty import PtyProcess

ROOT = r"D:\git_repos\GenericAgent"
COLS, ROWS = 100, 24
S1 = "ZEBRAFIRST"
S2 = "ZEBRASECOND"

env = dict(os.environ)
for k in [k for k in env if k.upper().startswith(("CLAUDE", "ANTHROPIC"))]:
    env.pop(k, None)
env["PYTHONPATH"] = ROOT
env["PYTHONIOENCODING"] = "utf-8"
env["PYTHONUTF8"] = "1"
env["TERM"] = "xterm-256color"

screen = pyte.Screen(COLS, ROWS)
stream = pyte.Stream(screen)
stop = threading.Event()


def screen_lines():
    try:
        buf = screen.buffer
        out = []
        for y in range(ROWS):
            row = buf.get(y)
            if not row:
                out.append("")
                continue
            out.append("".join(
                (row.get(x).data if row.get(x) and row.get(x).data else " ")
                for x in range(COLS)
            ).rstrip())
        return out
    except Exception:
        return None


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
        time.sleep(0)


threading.Thread(target=reader, daemon=True).start()
time.sleep(30)


def ask(text, wait):
    p.write(text)
    time.sleep(0.8)
    p.write("\r")
    time.sleep(wait)


ask(f"只回复这一个词，不要任何其他内容：{S1}", 25)
l1 = screen_lines() or []
in1 = any(S1 in l for l in l1)
print(f"轮 1 后屏幕含 {S1}: {in1}")

ask(f"只回复这一个词，不要任何其他内容：{S2}", 25)
l2 = screen_lines() or []
in1b = any(S1 in l for l in l2)
in2 = any(S2 in l for l in l2)

stop.set()
time.sleep(0.3)
try:
    p.terminate(force=True)
except Exception:
    pass

print(f"轮 2 后屏幕含 {S1}: {in1b}")
print(f"轮 2 后屏幕含 {S2}: {in2}")
print()
print(f"轮 2 后屏幕（{ROWS} 行）：")
for i, l in enumerate(l2):
    if l.strip():
        print(f"  {i:2d} | {l[:92]}")

print()
if in1b and in2:
    print("=> 真机 24 行未复现内容丢失：resizeReflow 的 BRAVO=0 是模型局限")
    sys.exit(0)
if in1 and not in1b:
    print("=> 真机 24 行【复现】内容丢失：spacer 方案有真回归，必须回滚")
    sys.exit(2)
print("=> 结果不明确")
sys.exit(3)
