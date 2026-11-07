# -*- coding: utf-8 -*-
"""真机验收：
  A. 默认模式发出的鼠标序列（应为 1000h+1006h，不含 1003h）
  B. 输入框仍固定在底部
  C. 滚轮/PgUp/End 仍工作
  D. 表格渲染（让模型输出一个表格）
"""
import hashlib
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
env.pop("GA_INK_MOUSE", None)
env.pop("GA_ENABLE_MOUSE_DRAG", None)
env["PYTHONPATH"] = ROOT
env["PYTHONIOENCODING"] = "utf-8"
env["PYTHONUTF8"] = "1"
env["TERM"] = "xterm-256color"

screen = pyte.Screen(COLS, ROWS)
stream = pyte.Stream(screen)
stop = threading.Event()
_last = {"row": -1}
series = []
raw_all = []


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


def body_first(lines):
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
        raw_all.append(raw if isinstance(raw, str) else raw.decode("utf-8", "replace"))
        try:
            stream.feed(raw)
        except Exception:
            pass
        series.append((time.time(), composer_row()))
        time.sleep(0)


threading.Thread(target=reader, daemon=True).start()
time.sleep(25)

joined = "".join(raw_all)
print("=== A. 鼠标序列 ===")
for pat in ["?1000h", "?1002h", "?1003h", "?1006h", "?1049h"]:
    print(f"  {pat}: {'YES' if pat in joined else 'no'}")
print(f"  → 期望 1000h+1006h 有、1003h 无: "
      f"{'✓' if ('?1000h' in joined and '?1006h' in joined and '?1003h' not in joined) else '✗'}")

boot_row = composer_row()
print(f"\n=== B. 输入框 ===\n  启动时第 {boot_row} 行 / {ROWS} 行 → {'贴底 ✓' if boot_row >= ROWS - 5 else '✗'}")

series.clear()
p.write("请输出一个 3 列 2 行的 markdown 表格，第一列是中文名称，直接用表格回答，不要其他文字。")
time.sleep(0.8)
p.write("\r")
time.sleep(45)

rows = [r for _, r in series if r >= 0]
moves = sum(1 for i in range(len(rows) - 1) if rows[i] != rows[i + 1])
print(f"\n=== B2. 流式期间 ===\n  输入框移动 {moves} 次 → {'不动 ✓' if moves == 0 else '✗'}")

lines = snap()
text = "\n".join(lines)
has_table = ("┌" in text and "│" in text and "└" in text)
print(f"\n=== D. 表格渲染 ===")
print(f"  出现制表符边框: {has_table}")
print("  屏幕相关行：")
for i, l in enumerate(lines):
    if any(ch in l for ch in "┌├└│"):
        print(f"    {i:2d} | {l[:96]}")

base = snap()
p.write("\x1b[5~")
time.sleep(1.2)
after_pgup = snap()
pgup_ok = fp(base) != fp(after_pgup)

p.write("\x1b[F")
time.sleep(1.2)
after_end = snap()
end_ok = fp(after_end) == fp(base)
print(f"\n=== C. 滚动控制 ===\n  PgUp: {'✓' if pgup_ok else '✗'}   End: {'✓' if end_ok else '✗'}")

stop.set()
time.sleep(0.3)
try:
    p.terminate(force=True)
except Exception:
    pass
