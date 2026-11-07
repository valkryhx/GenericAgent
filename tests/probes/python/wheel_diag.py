# -*- coding: utf-8 -*-
"""诊断滚轮：区分「滚轮键没送达」和「滚动被抵消」。

在 turn 完全结束后再滚轮（内容不再增长 => preserve... 不会抵消），
同时打印每次滚轮前后的屏幕首行，判断是否真的滚了。
"""
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


def top():
    for y in range(ROWS):
        if row_text(y).strip():
            return row_text(y)
    return ""


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

p.write("请用中文写一篇 60 行的技术说明，每行一句话，逐行编号 1 到 60。直接给正文。")
time.sleep(0.8)
p.write("\r")
time.sleep(60)   # 等到 turn 完全结束

print("=== turn 结束后 ===")
print(f"  首行: {top()[:70]!r}")

for label, seq in [
    ("PgUp", "\x1b[5~"),
]:
    print(f"\n--- 发 {label} ---")
    before = top()
    p.write(seq)
    time.sleep(1.0)
    after = top()
    print(f"  before: {before[:60]!r}")
    print(f"  after : {after[:60]!r}")
    print(f"  滚动: {before != after}")

print("\n--- 发 SGR 滚轮上 x5 ---")
before = top()
for _ in range(5):
    p.write("\x1b[<64;10;10M")
    time.sleep(0.15)
time.sleep(1.0)
after = top()
print(f"  before: {before[:60]!r}")
print(f"  after : {after[:60]!r}")
print(f"  滚动: {before != after}")

print("\n--- 发 End ---")
p.write("\x1b[F")
time.sleep(1.2)
print(f"  首行: {top()[:60]!r}")

stop.set()
time.sleep(0.3)
try:
    p.terminate(force=True)
except Exception:
    pass
