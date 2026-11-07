# -*- coding: utf-8 -*-
"""dump 实际屏幕内容，确认表格是否被渲染。"""
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
env.pop("GA_INK_MOUSE", None)
env["PYTHONPATH"] = ROOT
env["PYTHONIOENCODING"] = "utf-8"
env["PYTHONUTF8"] = "1"
env["TERM"] = "xterm-256color"

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
time.sleep(25)

PROMPT = (
    "请只输出下面这个 markdown 表格，不要任何解释文字、不要代码块围栏，直接以竖线开头：\n"
    "| 名称 | 延迟 | 说明 |\n"
    "| --- | --- | --- |\n"
    "| 缓存 | 12ms | 命中率高 |\n"
    "| 回源 | 180ms | 首次请求 |\n"
)
p.write(PROMPT.replace("\n", " "))
time.sleep(0.8)
p.write("\r")
time.sleep(50)

lines = [row_text(y) for y in range(ROWS)]
stop.set()
time.sleep(0.3)
try:
    p.terminate(force=True)
except Exception:
    pass

print("=== 屏幕内容 ===")
for i, l in enumerate(lines):
    if l.strip():
        print(f"  {i:2d} | {l}")

text = "\n".join(lines)
print()
print(f"含 ┌: {'┌' in text}   含 │: {'│' in text}   含 └: {'└' in text}")
print(f"含原始竖线表格行: {'| 名称' in text or '| 缓存' in text}")
