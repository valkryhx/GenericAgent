# -*- coding: utf-8 -*-
"""最终真机验收：不带任何环境变量，验默认模式。

R1 启动时输入框就在底部
R2 流式期间输入框位移次数
R3 PgUp 能翻历史
R4 End 能跳回底部
"""
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
env.pop("GA_INK_MOUSE", None)          # 不设 => 用默认值
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
        try:
            stream.feed(raw)
        except Exception:
            pass
        series.append((time.time(), composer_row()))
        time.sleep(0)


threading.Thread(target=reader, daemon=True).start()
time.sleep(30)

boot_row = composer_row()
print("=== R1: 启动时输入框位置（默认模式）===")
print(f"  下边框第 {boot_row} 行 / 终端 {ROWS} 行 → {'贴底 ✓' if boot_row >= ROWS - 5 else '未贴底 ✗'}")
print(f"  是否进入备用屏: 由 GA_INK_MOUSE 解析决定（默认 full）")

series.clear()
p.write("请用中文写一篇 55 行的技术说明，每行一句话，逐行编号 1 到 55。直接给正文。")
time.sleep(0.8)
p.write("\r")
time.sleep(60)

rows = [r for _, r in series if r >= 0]
moves = [(rows[i], rows[i + 1]) for i in range(len(rows) - 1) if rows[i] != rows[i + 1]]
print(f"\n=== R2: 流式期间（{len(rows)} 采样）===")
print(f"  输入框行号 {min(rows) if rows else '-'}~{max(rows) if rows else '-'}，移动 {len(moves)} 次"
      f" → {'不动 ✓' if not moves else '有移动 ✗'}")

base = snap()
print(f"\n=== R3/R4: 滚动控制 ===")
print(f"  当前首条内容: {body_first(base)[:56]!r}")

p.write("\x1b[5~")
time.sleep(1.2)
after_pgup = snap()
pgup_ok = fp(base) != fp(after_pgup)
print(f"  PgUp 后首条: {body_first(after_pgup)[:56]!r}  → 滚动 {'✓' if pgup_ok else '✗'}")

p.write("\x1b[F")
time.sleep(1.2)
after_end = snap()
end_ok = fp(after_end) == fp(base)
print(f"  End  后首条: {body_first(after_end)[:56]!r}  → 回到最新 {'✓' if end_ok else '✗'}")

stop.set()
time.sleep(0.3)
try:
    p.terminate(force=True)
except Exception:
    pass
