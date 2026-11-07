# -*- coding: utf-8 -*-
"""真机验收 full 模式 + 滚轮 + End 跳回底部。

三条要求逐条验：
  R1 启动时输入框就在底部（不需要等内容长高）
  R2 长内容滚动期间输入框不动
  R3 滚轮能翻历史；翻上去之后 End 能跳回底部
"""
import os
import re
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
_last = {"row": -1}
series = []
state = {"t0": None}


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


def composer_row():
    try:
        for y in range(ROWS - 1, -1, -1):
            t = row_text(y).strip()
            if len(t) >= 8 and set(t) == {"─"}:
                _last["row"] = y
                return y
    except Exception:
        pass
    return _last["row"]


def first_content_row():
    for y in range(ROWS):
        if row_text(y).strip():
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
        if state["t0"] is not None:
            series.append((time.time() - state["t0"], composer_row(), first_content_row()))
        time.sleep(0)


threading.Thread(target=reader, daemon=True).start()
time.sleep(30)

print("=== R1: 启动时输入框位置 ===")
boot_row = composer_row()
print(f"  输入框下边框第 {boot_row} 行 / 终端 {ROWS} 行  → {'贴底 ✓' if boot_row >= ROWS - 5 else '未贴底 ✗'}")

state["t0"] = time.time()
p.write("请用中文写一篇 50 行的技术说明，每行一句话，逐行编号 1 到 50。直接给正文。")
time.sleep(0.8)
p.write("\r")
time.sleep(55)
rows_a = [r for _, r, _ in series if r >= 0]
moves_a = [(rows_a[i], rows_a[i + 1]) for i in range(len(rows_a) - 1) if rows_a[i] != rows_a[i + 1]]
print(f"\n=== R2: 流式期间（{len(rows_a)} 采样）===")
print(f"  输入框行号 {min(rows_a) if rows_a else '-'}~{max(rows_a) if rows_a else '-'}，移动 {len(moves_a)} 次"
      f"  → {'不动 ✓' if not moves_a else '有移动 ✗'}")

# --- R3: 滚轮上翻 ---
series.clear()
state["t0"] = time.time()
top_before = first_content_row()
before_text = row_text(1)
for _ in range(8):
    p.write("\x1b[<64;10;10M")   # SGR wheel up
    time.sleep(0.12)
time.sleep(1.2)
top_after_wheel = first_content_row()
after_text = row_text(1)
print(f"\n=== R3a: 滚轮上翻 8 格 ===")
print(f"  屏幕首行内容: {before_text[:50]!r}")
print(f"           →  {after_text[:50]!r}")
print(f"  内容是否变化（= 滚动了）: {before_text != after_text}")

# --- R3b: End 跳回底部 ---
series.clear()
state["t0"] = time.time()
p.write("\x1b[F")   # End
time.sleep(1.5)
top_after_end = first_content_row()
end_text = row_text(1)
print(f"\n=== R3b: 按 End ===")
print(f"  屏幕首行内容: {end_text[:50]!r}")
print(f"  是否回到最新内容: {end_text != after_text or after_text == before_text}")
last_rows = [row_text(y) for y in range(ROWS)]
joined = "\n".join(last_rows)
print(f"  屏幕是否含最后编号 50: {'50.' in joined}")

stop.set()
time.sleep(0.3)
try:
    p.terminate(force=True)
except Exception:
    pass
