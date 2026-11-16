# -*- coding: utf-8 -*-
"""探针：真机里「原生光标 cell」与「输入框反显块 cell」是否重合（IME 锚点对齐）。

测什么
------
Windows Terminal 的输入法合成串/候选窗锚定在**可见的原生光标**所在的 cell 上
（见 docs/ga_ui_ime_visible_native_cursor_root_cause_2026-07-16.md）。因此只要原生
光标 cell ≠ 输入框自绘反显块 cell，中文合成时的预览就必然"看着没对齐输入框的光标"。

本探针在真 PTY 里跑 `./ga`，逐字输入 ASCII（不按 Enter，不触发 LLM），空闲后读回：
  - pyte 屏幕的原生光标 (row, col) 与 DECTCEM 记账；
  - 输入框 `> ` 那一行里**反显块**的列（pyte 的 reverse 属性）；
  - 两者的行差 / 列差。

判据
----
  DELTA=row 0 / col 0  → 对齐（IME 锚点与深色光标重合）
  否则                 → 不对齐；差值就是输入法候选框与深色光标之间的视觉偏移

为什么确定
----------
不依赖时序竞态：只在「空闲 ≥ IDLE 秒之后」采样一次，此时 GA 无任何待写帧，
原生光标停在最后一次 park 的位置。输入与采样之间有固定 sleep，重复 ROUNDS 轮。
读取用后台线程（winpty 的 read 会阻塞到有数据为止，主线程自己 sleep 更可控）。

为什么必须保留
--------------
2026-07-16 的修复只验证了"park 后 SHOW"这一条，从未验证 **park 的列**与反显块
**画出来的列**是否同一格。二者由两条独立代码路径算出（terminalCursor.ts 的
inputCursorPosition vs promptChrome.ts 的 renderInputLine + gutter 拼接），
本探针是唯一能钉住"两者必须相等"的机器判据。

运行：python tests/probes/python/_probe_ime_caret_alignment.py
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
IDLE = 1.6
ROUNDS = 3

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
ctr = {"show": 0, "hide": 0, "bytes": 0, "tail": ""}

p = PtyProcess.spawn(
    ["bash", "-lc", f"cd {ROOT.replace(os.sep, '/')} && ./ga"],
    dimensions=(ROWS, COLS), env=env, cwd=ROOT,
)


def reader():
    while not stop.is_set():
        try:
            raw = p.read(65536)
        except Exception:
            return
        if not raw:
            time.sleep(0.02)
            continue
        with lock:
            ctr["show"] += len(re.findall(r"\x1b\[\?25h", raw))
            ctr["hide"] += len(re.findall(r"\x1b\[\?25l", raw))
            ctr["bytes"] += len(raw)
            ctr["tail"] = (ctr["tail"] + raw)[-4000:]
            try:
                stream.feed(raw)
            except Exception:
                pass


th = threading.Thread(target=reader, daemon=True)
th.start()


def snap_screen():
    with lock:
        disp = list(screen.display)
        buf = {y: {x: screen.buffer[y][x] for x in range(COLS)} for y in range(ROWS)}
        cur = (screen.cursor.y, screen.cursor.x)
        counts = dict(ctr)
    return disp, buf, cur, counts


def composer_row(disp):
    """输入框上边框行：必须紧邻下面一行以 `>` 开头（gutter 的 `> `）。"""
    for y in range(len(disp) - 2, -1, -1):
        if re.fullmatch(r"─{8,}", disp[y].strip()) and disp[y + 1].lstrip().startswith(">"):
            return y
    return -1


def dump_screen(disp):
    print("  ── 屏幕快照 ──")
    for y, line in enumerate(disp):
        if line.strip():
            print(f"   {y:02d}|{line.rstrip()}")
    print("  ── end ──")


def snapshot(tag):
    disp, buf, cur, counts = snap_screen()
    top = composer_row(disp)
    row = top + 1 if top >= 0 else -1
    rev = []
    if row >= 0:
        rev = [(x, buf[row][x].data) for x in range(COLS) if buf[row][x].reverse]
    block = rev[0][0] if rev else None
    text = disp[row] if row >= 0 else ""
    if row < 0:
        dump_screen(disp)
    print(f"\n[{tag}]")
    print(f"  输入框上边框行 = {top}   输入行 = {row}")
    print(f"  输入行内容     = {text.rstrip()!r}")
    print(f"  反显块列       = {block}  ({len(rev)} 格 {rev[:3]})")
    print(f"  原生光标       = {cur}")
    print(f"  DECTCEM        = show {counts['show']} / hide {counts['hide']}  共 {counts['bytes']} B")
    if block is None:
        print("  → 该帧没有反显块（未画 caret）")
        return {"row": row, "block": None, "cur": cur}
    print(f"  → DELTA row={cur[0] - row}  col={cur[1] - block}")
    print("  ── 尾部字节（转义可见）──")
    print("   " + repr(counts["tail"][-1200:]))
    print("  ── end ──")
    return {"row": row, "block": block, "cur": cur}


try:
    for _ in range(60):
        time.sleep(0.5)
        if composer_row(snap_screen()[0]) >= 0:
            break
    time.sleep(1.0)

    results = []
    for i in range(ROUNDS):
        for ch in "abcde":
            try:
                p.write(ch)
            except Exception:
                pass
            time.sleep(0.12)
        time.sleep(IDLE)
        results.append(snapshot(f"第 {i + 1} 轮：输入 abcde 后空闲 {IDLE}s"))

    ok = all(
        r["block"] is not None and r["row"] >= 0
        and r["cur"][0] == r["row"] and r["cur"][1] == r["block"]
        for r in results
    )
    print("\n=== 结论 ===")
    for i, r in enumerate(results, 1):
        col_delta = None if r["block"] is None else r["cur"][1] - r["block"]
        print(f"  第 {i} 轮  row_delta={r['cur'][0] - r['row']}  col_delta={col_delta}")
    print("  OK  park 的原生光标与反显块重合（IME 锚点 = 深色光标）" if ok
          else "  NG  park 的原生光标与反显块不重合 —— 输入法锚点必然偏移")
finally:
    stop.set()
    try:
        p.terminate(force=True)
    except Exception:
        pass
    finish(_guard, 0)
