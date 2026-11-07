# -*- coding: utf-8 -*-
"""真机启动阶段逐次写入：记录每次写入后输入框行号与是否含 Ready.。

用户观察：full 模式启动时输入框在下面，但会被 Ready. 搞得移动一次。
本探针从进程启动的第 1 个字节开始记，不做任何 sleep 过滤。
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
env.pop("GA_INK_MOUSE", None)
env["PYTHONPATH"] = ROOT
env["PYTHONIOENCODING"] = "utf-8"
env["PYTHONUTF8"] = "1"
env["TERM"] = "xterm-256color"

screen = pyte.Screen(COLS, ROWS)
stream = pyte.Stream(screen)
stop = threading.Event()
records = []


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


def probe():
    composer = -1
    for y in range(ROWS - 1, -1, -1):
        t = row_text(y).strip()
        if len(t) >= 8 and set(t) == {"─"}:
            composer = y
            break
    lines = [row_text(y) for y in range(ROWS)]
    has_ready = any(l.strip() == "Ready." for l in lines)
    has_mcp = any("MCP" in l for l in lines)
    nonempty = sum(1 for l in lines if l.strip())
    return composer, has_ready, has_mcp, nonempty


p = PtyProcess.spawn(
    ["bash", "-lc", "cd /d/git_repos/GenericAgent && ./ga"],
    dimensions=(ROWS, COLS), env=env, cwd=ROOT,
)
t0 = time.time()


def reader():
    n = 0
    while not stop.is_set():
        try:
            raw = p.read(65536)
        except Exception:
            break
        if not raw:
            time.sleep(0.002)
            continue
        try:
            stream.feed(raw)
        except Exception:
            pass
        n += 1
        c, rd, mcp, ne = probe()
        records.append((time.time() - t0, n, c, rd, mcp, ne))
        time.sleep(0)


threading.Thread(target=reader, daemon=True).start()
time.sleep(25)
stop.set()
time.sleep(0.3)
try:
    p.terminate(force=True)
except Exception:
    pass

print(f"共 {len(records)} 次写入\n")
print("  时间     #  输入框  Ready?  MCP?  非空行")
prev = -999
for t, n, c, rd, mcp, ne in records:
    if c != prev or rd != records[records.index((t, n, c, rd, mcp, ne)) - 1][3] if records.index((t, n, c, rd, mcp, ne)) > 0 else True:
        pass
    mark = ""
    if c != prev:
        mark = "  <== 输入框移动"
        prev = c
    print(f"  {t:6.2f}s {n:3d}  {c:5d}   {'YES' if rd else '   '}    {'YES' if mcp else '   '}  {ne:4d}{mark}")

print("\n=== 输入框行号变化轨迹 ===")
prev = None
for t, n, c, rd, mcp, ne in records:
    if c != prev:
        print(f"  [{t:6.2f}s] #{n}  行号 {prev} -> {c}   (Ready={rd}, MCP={mcp}, 非空={ne})")
        prev = c
