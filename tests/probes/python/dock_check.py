# -*- coding: utf-8 -*-
"""满高 band 的真机验收：输入框是否固定在底部，历史是否还在。

关键风险：band 恒为 39 行（终端 40 行）时，Static 历史会被立刻顶出屏幕。
本探针在真机上分别看：
  1. 启动时输入框在第几行（期望：贴着底部）
  2. 跑完一轮后，第一轮的哨兵内容还在不在屏幕上
  3. 输入框行号在整轮里变不变
"""
import os
import sys
import threading
import time

import pyte
from winpty import PtyProcess

ROOT = r"D:\git_repos\GenericAgent"
COLS, ROWS = 110, 40
S1 = "ALPHAONE"

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
_last = {"row": -1}
series = []


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
        with_lock = None  # noqa: F841
        r = composer_row()
        series.append((time.time(), r))
        time.sleep(0)


def snapshot():
    return [row_text(y) for y in range(ROWS)]


threading.Thread(target=reader, daemon=True).start()
time.sleep(30)

boot = snapshot()
boot_row = composer_row()
nonempty = [i for i, l in enumerate(boot) if l.strip()]
print(f"=== 启动时 ===")
print(f"  输入框（下边框）在第 {boot_row} 行 / 终端 {ROWS} 行")
print(f"  非空行 {len(nonempty)} 行，范围 {min(nonempty) if nonempty else '-'}~{max(nonempty) if nonempty else '-'}")
for i in nonempty[:6]:
    print(f"    {i:2d} | {boot[i][:88]}")

t0 = time.time()
p.write(f"只回复这一个词，不要其他内容：{S1}")
time.sleep(0.8)
p.write("\r")
time.sleep(30)

after = snapshot()
after_rows = [r for _, r in series if r >= 0]
has_sentinel = any(S1 in l for l in after)

stop.set()
time.sleep(0.3)
try:
    p.terminate(force=True)
except Exception:
    pass

print(f"\n=== 跑完一轮后 ===")
print(f"  第一轮哨兵 {S1} 是否还在屏幕: {has_sentinel}")
print(f"  输入框行号范围: {min(after_rows) if after_rows else '-'}~{max(after_rows) if after_rows else '-'}")
moves = [(i, after_rows[i], after_rows[i + 1])
         for i in range(len(after_rows) - 1) if after_rows[i] != after_rows[i + 1]]
print(f"  输入框移动 {len(moves)} 次")

ne = [i for i, l in enumerate(after) if l.strip()]
print(f"  非空行 {len(ne)} 行")
print(f"\n  屏幕内容（前 14 个非空行）：")
for i in ne[:14]:
    print(f"    {i:2d} | {after[i][:88]}")
