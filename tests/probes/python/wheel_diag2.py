# -*- coding: utf-8 -*-
"""诊断滚轮（修正版）：比较整屏指纹，不比较固定标题行。"""
import hashlib
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


def snap():
    return [row_text(y) for y in range(ROWS)]


def fp(lines):
    return hashlib.md5("\n".join(lines).encode("utf-8", "replace")).hexdigest()[:8]


def body_first(lines):
    """第 2 行起的第一条非空内容（跳过标题栏）。"""
    for y in range(1, len(lines)):
        if lines[y].strip():
            return lines[y]
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
time.sleep(60)

base = snap()
print("=== turn 结束后 ===")
print(f"  首条内容: {body_first(base)[:70]!r}")


def try_seq(label, seq, repeat=1, gap=0.15):
    before = snap()
    for _ in range(repeat):
        p.write(seq)
        time.sleep(gap)
    time.sleep(1.0)
    after = snap()
    changed = fp(before) != fp(after)
    print(f"\n--- {label} ---")
    print(f"  before: {body_first(before)[:62]!r}")
    print(f"  after : {body_first(after)[:62]!r}")
    print(f"  整屏变化: {changed}")
    return changed


try_seq("PgUp (CSI 5~)", "\x1b[5~")
try_seq("SGR wheel up x5", "\x1b[<64;10;10M", repeat=5)
try_seq("End (CSI F)", "\x1b[F")

stop.set()
time.sleep(0.3)
try:
    p.terminate(force=True)
except Exception:
    pass
