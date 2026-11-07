# -*- coding: utf-8 -*-
"""决定性对照：短答案 vs 长答案，输入框行号的时间行为。

假设（由前几轮测量推出）：
  内联模式下 画面总行数 = Static 历史 + 动态帧 + 底栏，
  动态帧高度随流式内容增长，输入框被一路往下推；
  一旦内容撑满终端高度，终端开始滚动，输入框停在底部行号不再动。

若假设成立：
  - 短答案（内容 < 终端高度）：输入框持续下移，全程不稳定  ← 用户感到「不平稳」
  - 长答案（内容 > 终端高度）：撑满后输入框稳定在底部行号  ← 用户感到「平稳」

这能解释用户说的「偶尔轻微上下位移」：位移发生在撑满之前的那段时间。
"""
import os
import re
import sys
import threading
import time

import pyte
from winpty import PtyProcess

ROOT = r"D:\git_repos\GenericAgent"
COLS, ROWS = 120, 40

env = dict(os.environ)
for k in [k for k in env if k.upper().startswith(("CLAUDE", "ANTHROPIC"))]:
    env.pop(k, None)
env["PYTHONPATH"] = ROOT
env["PYTHONIOENCODING"] = "utf-8"
env["PYTHONUTF8"] = "1"
env["TERM"] = "xterm-256color"


def composer_row(screen):
    disp = screen.display
    for y in range(len(disp) - 1, -1, -1):
        stripped = disp[y].strip()
        if re.fullmatch(r"─{8,}", stripped):
            return y
    return -1


def run_case(label, prompt, wait_s):
    screen = pyte.Screen(COLS, ROWS)
    stream = pyte.Stream(screen)
    series = []
    lock = threading.Lock()
    stop = threading.Event()
    state = {"t0": None}

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
            if state["t0"] is None:
                continue
            with lock:
                series.append((time.time() - state["t0"], composer_row(screen)))
            time.sleep(0)

    th = threading.Thread(target=reader, daemon=True)
    th.start()
    time.sleep(30)

    p.write(prompt)
    time.sleep(0.8)
    p.write("\r")
    state["t0"] = time.time()
    time.sleep(wait_s)
    stop.set()
    time.sleep(0.3)
    try:
        p.terminate(force=True)
    except Exception:
        pass

    with lock:
        got = [(t, r) for t, r in series if r >= 0]

    print(f"\n{'=' * 62}\n{label}  (采样 {len(got)} 个)\n{'=' * 62}")
    if not got:
        print("  无数据")
        return None

    rows = [r for _, r in got]
    print(f"  行号范围 {min(rows)} ~ {max(rows)}  终端高度 {ROWS}")
    moves = [(got[i][0], rows[i], rows[i + 1])
             for i in range(len(rows) - 1) if rows[i] != rows[i + 1]]
    print(f"  移动 {len(moves)} 次")
    for t, a, b in moves:
        print(f"    [{t:6.2f}s] {a:2d} -> {b:2d}  ({b - a:+d})")

    # 到达过的最大行号之后是否稳定
    peak = rows.index(max(rows))
    after = rows[peak:]
    tail_moves = sum(1 for i in range(len(after) - 1) if after[i] != after[i + 1])
    print(f"  到达最高行号 {max(rows)} 后，仍有 {tail_moves} 次移动")
    hit_bottom = max(rows) >= ROWS - 4
    print(f"  是否撑到底部(>={ROWS - 4}): {'是' if hit_bottom else '否'}")
    return {"max": max(rows), "moves": len(moves), "tail": tail_moves, "bottom": hit_bottom}


short = run_case(
    "短答案（约 3 段）",
    "请用中文写 1 段话介绍终端渲染，2 句即可。直接给正文，不要用工具。",
    40,
)
long_ = run_case(
    "长答案（要求 30 行以上）",
    "请用中文写一篇技术说明，至少 40 行，每行一句话，逐行编号。直接给正文，不要用工具。",
    70,
)

print(f"\n{'=' * 62}\n对照结论\n{'=' * 62}")
if short and long_:
    print(f"  短答案：最高行号 {short['max']:2d}  移动 {short['moves']:2d} 次  到底部={short['bottom']}")
    print(f"  长答案：最高行号 {long_['max']:2d}  移动 {long_['moves']:2d} 次  到底部={long_['bottom']}")
    print()
    if long_["bottom"] and not short["bottom"]:
        print("  => 假设成立：撑满终端后输入框贴底稳定，位移只发生在「撑满之前」")
        print("     用户感知的「偶尔轻微上下位移」= 每轮输出撑满屏幕前的那几秒")
    elif long_["bottom"] and short["bottom"]:
        print("  => 两者都贴底：位移不是高度增长引起，需另找原因")
    else:
        print("  => 都没贴底：终端的 40 行对这种内容量偏大，需缩小终端复现")
