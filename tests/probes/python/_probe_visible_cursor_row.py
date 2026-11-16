# -*- coding: utf-8 -*-
"""探针：真机里「可见原生光标」每次出现时，到底落在哪一行（IME 锚点的真实坐标）。

测什么
------
Windows Terminal 把输入法合成串（preedit）与候选框画在**可见的原生光标**
（DECTCEM 置位时的那个 cell）上。所以用户看到"拼音跑到屏幕下方"等价于：
`ESC[?25h` 之后的那个 cell 不在 composer 的输入行里。

本探针逐字节解析真 PTY 输出（pyte 做屏幕回放，自己记账 DECTCEM），在每一次
`ESC[?25h` 之后立刻采样：
  - 原生光标的绝对 (row, col)
  - 该行内容
  - 该行是不是 composer 的输入行（`>` gutter 或反显块所在行）

判据
----
  SHOW 后光标 100% 落在 composer 输入行 → 对齐（用户不该看到拼音漂移）
  否则                                    → 不对齐，落点就是 preedit 的显示位置

为什么确定
----------
只看"SHOW 之后"的状态，不看中间态；SHOW 是终端开始显示光标的唯一时点，与
park/可见性契约（`stdoutCursorPark.ts:190`）严格对应。ConPTY 会改写绝对定位与
擦除序列，但 `?25h` 本身与相对移动会保留。

为什么必须保留
--------------
现有 `park_absolute_row.ts` 是 headless（CaptureWriteStream），2026-07-16 之后
仓库默认切到 **fullscreen/alt-screen**（`mouseWheel.ts:112` 恒返回 true），
headless 与真机的渲染路径已经不同；本探针是真机上唯一直接测"可见光标落在哪一行"
的判据。

运行：python tests/probes/python/_probe_visible_cursor_row.py
"""
import os
import re
import sys
import threading
import time
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pyte
from winpty import PtyProcess

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _workspace_guard import finish, start  # noqa: E402

ROOT = r"D:\git_codes\GenericAgent"
COLS, ROWS = 120, 40
IDLE = 1.8

_guard = start(ROOT, quiet=True)

env = dict(os.environ)
for k in [k for k in env if k.upper().startswith(("CLAUDE", "ANTHROPIC"))]:
    env.pop(k, None)
env["PYTHONPATH"] = ROOT
env["PYTHONIOENCODING"] = "utf-8"
env["PYTHONUTF8"] = "1"
env["TERM"] = "xterm-256color"

screen = pyte.Screen(COLS, ROWS)
stream = pyte.Stream(screen)
lock = threading.Lock()
stop = threading.Event()
acc = {"shows": [], "hide_ct": 0, "show_ct": 0, "bytes": 0}

p = PtyProcess.spawn(
    ["bash", "-lc", f"cd {ROOT.replace(os.sep, '/')} && ./ga"],
    dimensions=(ROWS, COLS), env=env, cwd=ROOT,
)


def feed_bytewise(raw):
    """逐段喂给 pyte，并在每个 ?25h 之后记录一次光标位置。"""
    text = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
    with lock:
        acc["bytes"] += len(text)
        acc["show_ct"] += len(re.findall(r"\x1b\[\?25h", text))
        acc["hide_ct"] += len(re.findall(r"\x1b\[\?25l", text))
        parts = re.split(r"(\x1b\[\?25h)", text)
        for part in parts:
            if part == "\x1b[?25h":
                try:
                    stream.feed(part)
                except Exception:
                    pass
                disp = list(screen.display)
                row = screen.cursor.y
                acc["shows"].append({
                    "abs_row": row,
                    "col": screen.cursor.x,
                    "line": disp[row] if 0 <= row < len(disp) else "",
                    "disp": disp,
                })
                continue
            if not part:
                continue
            try:
                stream.feed(part)
            except Exception:
                pass


def reader():
    while not stop.is_set():
        try:
            raw = p.read(65536)
        except Exception:
            return
        if not raw:
            time.sleep(0.02)
            continue
        feed_bytewise(raw)


threading.Thread(target=reader, daemon=True).start()


def classify(rec):
    line = rec["line"]
    if re.search(r"^\s*>\s", line):
        return "composer"
    if re.search(r"─{8,}", line):
        return "border"
    if ">" in line[:6]:
        return "composer"
    return "other"


try:
    time.sleep(9.0)  # 启动 + MCP 发现
    for ch in "abcde":
        p.write(ch)
        time.sleep(0.15)
    time.sleep(IDLE)
    for ch in ("\x7f", "\x7f"):
        p.write(ch)
        time.sleep(0.15)
    time.sleep(IDLE)

    with lock:
        shows = list(acc["shows"])
        stats = {k: acc[k] for k in ("show_ct", "hide_ct", "bytes")}

    print(f"共 {stats['bytes']} 字节  SHOW={stats['show_ct']}  HIDE={stats['hide_ct']}  采样SHOW={len(shows)}")
    print()
    kinds = {}
    for rec in shows[-14:]:
        kind = classify(rec)
        kinds[kind] = kinds.get(kind, 0) + 1
        print(f"  SHOW → abs_row={rec['abs_row']:2d} col={rec['col']:3d}  [{kind}]  {rec['line'].rstrip()[:100]!r}")
    print()
    print("  采样分布:", kinds)

    # 最后一张屏幕，标出 composer 与光标
    if shows:
        disp = shows[-1]["disp"]
        print("\n  最终屏幕（★=可见光标所在行）:")
        for y, line in enumerate(disp):
            if line.strip():
                mark = "★" if y == shows[-1]["abs_row"] else " "
                print(f"   {mark}{y:02d}|{line.rstrip()[:110]}")
finally:
    stop.set()
    try:
        p.terminate(force=True)
    except Exception:
        pass
    finish(_guard, 0)
