# -*- coding: utf-8 -*-
"""用真实终端仿真器（pyte）回放 GA Ink TUI 的输出，量化三件事：

  1. 输入框底边是否在每一帧都停在同一行（= 不抖）
  2. 应用占用的行数在帧与帧之间是否恒定（= 不抖的根因）
  3. 屏幕上有多少行被重写过（= 闪不闪的代理指标）

判据不依赖"看起来如何"，而是数每条吃进去的字节流后，光标行号怎么变。

用法：python .tmp_ga_jitter_probe.py [场景]
  场景 boot    —— 只启动，不交互（测启动稳定性）
  场景 chat    —— 启动后打一句话并回车（测流式输出期间的抖动）
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
SCENARIO = sys.argv[1] if len(sys.argv) > 1 else "chat"
PROMPT = "Count from 1 to 20, one number per line."

env = dict(os.environ)
for k in [k for k in env if k.upper().startswith(("CLAUDE", "ANTHROPIC"))]:
    env.pop(k, None)
env["PYTHONPATH"] = ROOT
env["PYTHONIOENCODING"] = "utf-8"
env["PYTHONUTF8"] = "1"
env["TERM"] = "xterm-256color"

screen = pyte.Screen(COLS, ROWS)
stream = pyte.Stream(screen)

p = PtyProcess.spawn(
    ["bash", "-lc", "cd /d/git_repos/GenericAgent && ./ga"],
    dimensions=(ROWS, COLS),
    env=env,
    cwd=ROOT,
)

samples = []          # (t, cursor_y, cursor_x, nonblank_rows, bottom_ui_top)
lock = threading.Lock()
stop = threading.Event()


def ui_signature():
    """找出输入框（含 '>' 提示符的那一行）与底栏的位置。"""
    disp = screen.display
    prompt_row = None
    for y in range(len(disp) - 1, -1, -1):
        if disp[y].lstrip().startswith(">"):
            prompt_row = y
            break
    nonblank = sum(1 for line in disp if line.strip())
    return prompt_row, nonblank


def reader():
    while not stop.is_set():
        try:
            raw = p.read(65536)
        except EOFError:
            break
        except Exception:
            break
        if not raw:
            time.sleep(0.02)
            continue
        try:
            stream.feed(raw)
        except Exception:
            pass
        with lock:
            pr, nb = ui_signature()
            samples.append((time.time(), screen.cursor.y, screen.cursor.x, nb, pr))


th = threading.Thread(target=reader, daemon=True)
th.start()

t0 = time.time()
# 等启动 + MCP
time.sleep(32)

if SCENARIO == "chat":
    p.write(PROMPT)
    time.sleep(1.0)
    p.write("\r")
    time.sleep(45)

stop.set()
time.sleep(0.5)
try:
    p.terminate(force=True)
except Exception:
    pass

print("samples:", len(samples))
if not samples:
    sys.exit(1)

prompt_rows = [s[4] for s in samples if s[4] is not None]
nonblanks = [s[3] for s in samples]

print()
print("=== 输入框位置（' >' 那一行）===")
if prompt_rows:
    uniq = sorted(set(prompt_rows))
    print("  出现过的行号集合:", uniq)
    print("  是否恒定        :", "YES（不抖）" if len(uniq) == 1 else "NO —— 抖了")
    # 连续段统计
    flips = sum(1 for a, b in zip(prompt_rows, prompt_rows[1:]) if a != b)
    print("  相邻样本位置变化次数:", flips)
else:
    print("  未识别到输入框")

print()
print("=== 应用占用行数 ===")
print("  最小/最大:", min(nonblanks), "/", max(nonblanks))
print("  是否恒定 :", "YES" if min(nonblanks) == max(nonblanks) else "NO —— 画面总高在变")

print()
print("=== 最终屏幕（去空行）===")
for y, line in enumerate(screen.display):
    if line.strip():
        print("%2d| %s" % (y, line.rstrip()))
