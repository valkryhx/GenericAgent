# -*- coding: utf-8 -*-
"""真机验收 full 模式（备用屏）：输入框是否从一开始就在底部、滚动时是否位移。

用户要求：
  1. 启动时输入框就在应在的位置（底部），不是先在上面再往下走
  2. 内容滚动时输入框不位移

inline 模式（<Static> + 动态帧）在结构上做不到这两点：Static 历史写在上方，
历史一涨帧就被推下去，终端滚满后 Static 写出又会带着整帧滚动。
本探针测 GA_INK_MOUSE=full 是否满足。
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
env["GA_INK_MOUSE"] = "full"

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
        series.append((time.time(), composer_row()))
        time.sleep(0)


def snapshot():
    return [row_text(y) for y in range(ROWS)]


threading.Thread(target=reader, daemon=True).start()
time.sleep(30)

boot = snapshot()
boot_row = composer_row()
ne = [i for i, l in enumerate(boot) if l.strip()]
print("=== full 模式：启动时 ===")
print(f"  输入框（下边框）第 {boot_row} 行 / 终端 {ROWS} 行")
print(f"  非空行 {len(ne)} 行，范围 {min(ne) if ne else '-'}~{max(ne) if ne else '-'}")
for i in ne[:8]:
    print(f"    {i:2d} | {boot[i][:88]}")

p.write(f"只回复这一个词，不要其他内容：{S1}")
time.sleep(0.8)
p.write("\r")
time.sleep(30)

after = snapshot()
rows = [r for _, r in series if r >= 0]
moves = [(i, rows[i], rows[i + 1]) for i in range(len(rows) - 1) if rows[i] != rows[i + 1]]
has_sentinel = any(S1 in l for l in after)

stop.set()
time.sleep(0.3)
try:
    p.terminate(force=True)
except Exception:
    pass

print("\n=== full 模式：跑完一轮后 ===")
print(f"  哨兵 {S1} 是否在屏幕: {has_sentinel}")
print(f"  输入框行号范围: {min(rows) if rows else '-'}~{max(rows) if rows else '-'}")
print(f"  移动 {len(moves)} 次")
neo = [i for i, l in enumerate(after) if l.strip()]
print(f"  非空行 {len(neo)} 行")
for i in neo[:12]:
    print(f"    {i:2d} | {after[i][:88]}")
