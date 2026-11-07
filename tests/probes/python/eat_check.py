# -*- coding: utf-8 -*-
"""真机验证：spacer 改动会不会把上一轮的回答吃掉。

resizeReflow 模型报 BRAVO=0（上一轮内容被擦掉）。但那个模型是简化实现，
需要用真机 PTY 判断这是真回归还是模型产物。

做法：40 行终端，跑两轮，每轮让模型回一个独特哨兵词，
第二轮结束后检查第一轮的哨兵是否仍在屏幕上。
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
SENTINEL_1 = "ZEBRAFIRST"
SENTINEL_2 = "ZEBRASECOND"

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


def screen_text():
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


th = threading.Thread(target=reader, daemon=True)
th.start()
time.sleep(30)


def ask(prompt, wait):
    p.write(prompt)
    time.sleep(0.8)
    p.write("\r")
    time.sleep(wait)


ask(f"只回复这一个词，不要任何其他内容：{SENTINEL_1}", 25)
lines1 = screen_text() or []
has1_after_turn1 = any(SENTINEL_1 in l for l in lines1)
print(f"轮 1 结束后，屏幕上能见到 {SENTINEL_1}: {has1_after_turn1}")

ask(f"只回复这一个词，不要任何其他内容：{SENTINEL_2}", 25)
lines2 = screen_text() or []
has1_after_turn2 = any(SENTINEL_1 in l for l in lines2)
has2_after_turn2 = any(SENTINEL_2 in l for l in lines2)

stop.set()
time.sleep(0.3)
try:
    p.terminate(force=True)
except Exception:
    pass

print(f"轮 2 结束后，屏幕上能见到 {SENTINEL_1}: {has1_after_turn2}")
print(f"轮 2 结束后，屏幕上能见到 {SENTINEL_2}: {has2_after_turn2}")
print()
print("轮 2 结束后的屏幕（非空行）：")
for i, l in enumerate(lines2):
    if l.strip():
        print(f"  {i:2d} | {l[:100]}")

print()
if has1_after_turn2 and has2_after_turn2:
    print("=> 真机未复现内容被吃掉：resizeReflow 的 BRAVO=0 是模型局限")
elif not has1_after_turn2 and has1_after_turn1:
    print("=> 真机复现：上一轮内容被擦掉，spacer 改动有真回归，必须回滚")
else:
    print("=> 结果不明确，需要再看")
