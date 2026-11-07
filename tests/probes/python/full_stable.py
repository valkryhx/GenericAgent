# -*- coding: utf-8 -*-
"""full 模式稳定后的位移检测：跳过启动阶段，只看内容滚动期间。

判据（用户能感知的）：
  1. 启动完成后，输入框是否已经贴底且不再动
  2. 长内容滚动期间，输入框行号是否变过
  3. 滚轮滚动历史后，输入框是否仍然不动
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
        r = composer_row()
        if state["t0"] is not None:
            series.append((time.time() - state["t0"], r))
        time.sleep(0)


threading.Thread(target=reader, daemon=True).start()
time.sleep(30)

boot_row = composer_row()
print(f"=== full 模式 ===")
print(f"  启动完成时输入框在第 {boot_row} 行 / 终端 {ROWS} 行")

# 阶段 A：长内容滚动
state["t0"] = time.time()
p.write("请用中文写一篇 50 行的技术说明，每行一句话，逐行编号 1 到 50。直接给正文。")
time.sleep(0.8)
p.write("\r")
time.sleep(55)

a_rows = [r for _, r in series if r >= 0]
a_moves = [(a_rows[i], a_rows[i + 1]) for i in range(len(a_rows) - 1) if a_rows[i] != a_rows[i + 1]]
print(f"\n  A. 长内容滚动期间（{len(a_rows)} 采样）:")
print(f"     行号范围 {min(a_rows) if a_rows else '-'}~{max(a_rows) if a_rows else '-'}，移动 {len(a_moves)} 次")
if a_moves:
    from collections import Counter
    print(f"     移动对: {dict(Counter(a_moves))}")

# 阶段 B：滚轮往上翻历史
state["t0"] = time.time()
series.clear()
# 滚轮上 = ESC[<64;1;1M (SGR) ；为兼容再发传统 ESC[M + 0x60
for _ in range(6):
    p.write("\x1b[<64;10;10M")
    time.sleep(0.12)
time.sleep(1.5)

b_rows = [r for _, r in series if r >= 0]
b_moves = sum(1 for i in range(len(b_rows) - 1) if b_rows[i] != b_rows[i + 1])
print(f"\n  B. 滚轮上翻历史后（{len(b_rows)} 采样）:")
print(f"     行号范围 {min(b_rows) if b_rows else '-'}~{max(b_rows) if b_rows else '-'}，移动 {b_moves} 次")

# 看内容是否真的滚动了（第一行内容变了没）
top_after = row_text(1)

# 阶段 C：End / jump to bottom
state["t0"] = time.time()
series.clear()
p.write("\x1b[F")   # End
time.sleep(0.3)
p.write("\x1b[4~")  # End (vt)
time.sleep(1.5)

c_rows = [r for _, r in series if r >= 0]
c_moves = sum(1 for i in range(len(c_rows) - 1) if c_rows[i] != c_rows[i + 1])
print(f"\n  C. 按 End 之后（{len(c_rows)} 采样）:")
print(f"     行号范围 {min(c_rows) if c_rows else '-'}~{max(c_rows) if c_rows else '-'}，移动 {c_moves} 次")

stop.set()
time.sleep(0.3)
try:
    p.terminate(force=True)
except Exception:
    pass

lines = [row_text(y) for y in range(ROWS)]
print("\n  最终屏幕前 4 行：")
for i in range(4):
    print(f"    {i:2d} | {lines[i][:86]}")
