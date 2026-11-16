# -*- coding: utf-8 -*-
"""探针：真机上「可见原生光标」的位置随时间的分布（IME 锚点抖动 = 左右/上下晃的量化）。

测什么
------
以 8ms 粒度采样 pyte 光标绝对位置，按「是否落在 composer 的 caret cell 上」分类，
并把每一次位置变化连同该行内容打出来。

背景 / 根因坐标
---------------
`stdoutCursorPark.ts:150-198` 的 flush 是「unpark + 整帧 + park」一次原子写，park 段
把光标相对移到 caret。写入本身经 `_probe_park_spec_real.py` 证实是对的
（`spec={"up":4,"col":8}` 与输入行/反显块精确一致）。因此若屏幕上的光标不在 caret，
只能是 **park 之后又有写入把它挪走**，或 park 段没落到最终写入上。

为什么确定
----------
采样与写入解耦：只看屏幕状态，不看 GA 的自我报告。同一行重复出现即「静止」，出现
多个不同 row 即「晃动」，并给出每个位置的停留时长占比。

为什么必须保留
--------------
用户报告的是「输入中文时光标左右晃、拼音跑屏幕下方」。这两句都是**随时间**的现象，
任何一次性快照（现有 `park_absolute_row.ts` 即是）都无法区分「一直错」与「在几个
位置之间跳」。本探针是唯一能提供「停留时长分布」的判据。

运行：python tests/probes/python/_probe_cursor_jitter_timeline.py
"""
import os
import re
import sys
import threading
import time
from collections import Counter
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import pyte
from winpty import PtyProcess

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _workspace_guard import finish, start  # noqa: E402

ROOT = r"D:\git_codes\GenericAgent"
COLS, ROWS = 120, 40
SAMPLE_MS = 8

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
bytes_ct = {"n": 0}

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
            time.sleep(0.005)
            continue
        with lock:
            bytes_ct["n"] += len(raw)
            try:
                stream.feed(raw)
            except Exception:
                pass


threading.Thread(target=reader, daemon=True).start()


def caret_cell():
    """composer 输入行的 (row, col)：`>` 后第一个非 gutter 字符之后的位置。"""
    with lock:
        disp = list(screen.display)
        buf = {y: {x: screen.buffer[y][x] for x in range(COLS)} for y in range(min(ROWS, len(screen.buffer)))}
        cur = (screen.cursor.y, screen.cursor.x)
    top = -1
    for y in range(len(disp) - 2, -1, -1):
        if re.fullmatch(r"─{8,}", disp[y].strip()) and disp[y + 1].lstrip().startswith(">"):
            top = y
            break
    if top < 0:
        return None, cur, disp
    row = top + 1
    rev = [x for x in range(COLS) if buf.get(row, {}).get(x) is not None and buf[row][x].reverse]
    col = rev[0] if rev else None
    return (row, col), cur, disp


def sample_window(seconds, tag):
    """采样一段时间，返回 {(row,col): 停留毫秒}。"""
    dwell = Counter()
    transitions = []
    t0 = time.time()
    last = None
    while time.time() - t0 < seconds:
        _, cur, _ = caret_cell()
        dwell[cur] += SAMPLE_MS
        if cur != last:
            transitions.append((round((time.time() - t0) * 1000), cur))
            last = cur
        time.sleep(SAMPLE_MS / 1000.0)
    print(f"\n[{tag}] 采样 {seconds}s  位置种类={len(dwell)}")
    for pos, ms in dwell.most_common(8):
        print(f"   {str(pos):>12}  停留 {ms:6d} ms  ({ms / (seconds * 1000) * 100:.1f}%)")
    if len(transitions) <= 25:
        print("   位置序列:", " → ".join(str(t[1]) for t in transitions))
    else:
        print(f"   位置变化 {len(transitions)} 次（前 20）:", " → ".join(str(t[1]) for t in transitions[:20]))
    return dwell, transitions


try:
    for _ in range(80):
        time.sleep(0.4)
        cell, cur, _ = caret_cell()
        if cell is not None:
            break
    time.sleep(1.5)

    cell, cur, disp = caret_cell()
    print(f"composer caret cell = {cell}   当前原生光标 = {cur}")

    sample_window(2.0, "A 空闲")

    for ch in "abc":
        p.write(ch)
        time.sleep(0.18)
    sample_window(1.2, "B 刚输入 abc（编辑器活跃）")

    sample_window(2.5, "C 输入后长时间空闲")

    cell, cur, disp = caret_cell()
    print(f"\n最终 composer caret cell = {cell}   原生光标 = {cur}")
    print("\n最终屏幕:")
    for y, line in enumerate(disp):
        if line.strip():
            mark = "★" if y == cur[0] else " "
            print(f"  {mark}{y:02d}|{line.rstrip()[:112]}")
finally:
    stop.set()
    try:
        p.terminate(force=True)
    except Exception:
        pass
    finish(_guard, 0)
