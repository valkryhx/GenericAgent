# -*- coding: utf-8 -*-
"""探针：ConPTY 是否保留「相对光标移动 + DECTCEM SHOW」（对照组 + 实验组）。

测什么
------
在真 PTY 里跑 node，分别写出：
  A 对照组：`AAAA` 后用**绝对 CUP** `ESC[1;3H` 定位并 SHOW
  B 实验组：`AAAA / BBBB / CCCC` 后用 GA 相同的**相对移动** `ESC[2A ESC[3G` 定位并 SHOW
然后由 pyte 回放 PTY 输出，读回光标 cell。

为什么需要它
------------
`_probe_park_spec_real.py` 已证实 GA writer 在帧末确实发出 `ESC[4A ESC[9G`，
但同一时刻真 PTY 上读到的光标在屏幕右下角。要么相对移动被 ConPTY 改写，要么
pyte 解析有偏。本探针用「同样的 pyte、只换序列」把这两个变量分开：

  A（绝对）对、B（相对）错  → ConPTY 改写相对移动，GA 的 park 在 WT 上可能失效
  A、B 都对                 → park 有效，真机偏移另有原因（DECTCEM / 帧后写入）
  A、B 都错                 → pyte 在 ConPTY 下的解析不可信，测量方法要换

为什么必须保留
--------------
这是唯一把「GA 的 park 算术」与「ConPTY 的行为」解耦的对照实验。GA 在
fullscreen/alt-screen 下用相对 park（`stdoutCursorPark.ts:190`），而 CC 319 在
alt-screen 下用绝对 CUP（`m0701:16294`）——若本探针证明相对在 ConPTY 上不保留，
这就是必须切绝对路径的直接证据。

运行：python tests/probes/python/_probe_conpty_cursor_control.py
"""
import os
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
COLS, ROWS = 80, 24

_guard = start(ROOT, quiet=True)

CASES = {
    "A 主屏·绝对 CUP ESC[1;3H": (
        "process.stdout.write('AAAA\\n');"
        "process.stdout.write('\\u001b[1;3H');"
        "process.stdout.write('\\u001b[?25h');"
        "setTimeout(()=>{}, 300)"
    ),
    "B 主屏·相对 ESC[2A ESC[3G": (
        "process.stdout.write('AAAA\\nBBBB\\nCCCC\\n');"
        "process.stdout.write('\\u001b[2A\\u001b[3G');"
        "process.stdout.write('\\u001b[?25h');"
        "setTimeout(()=>{}, 300)"
    ),
    "C alt-screen·相对 ESC[2A ESC[3G": (
        "process.stdout.write('\\u001b[?1049h\\u001b[2J\\u001b[H');"
        "process.stdout.write('AAAA\\nBBBB\\nCCCC\\n');"
        "process.stdout.write('\\u001b[2A\\u001b[3G');"
        "process.stdout.write('\\u001b[?25h');"
        "setTimeout(()=>{}, 300)"
    ),
    "D alt-screen+BSU·相对（GA 完整形态）": (
        "process.stdout.write('\\u001b[?1049h\\u001b[2J\\u001b[H');"
        "process.stdout.write('\\u001b[?2026h');"
        "process.stdout.write('AAAA\\nBBBB\\nCCCC\\n');"
        "process.stdout.write('\\u001b[2A\\u001b[3G');"
        "process.stdout.write('\\u001b[?2026l');"
        "process.stdout.write('\\u001b[?25h');"
        "setTimeout(()=>{}, 300)"
    ),
}


def run_case(name, script):
    env = dict(os.environ)
    for k in [k for k in env if k.upper().startswith(("CLAUDE", "ANTHROPIC"))]:
        env.pop(k, None)
    env["TERM"] = "xterm-256color"

    screen = pyte.Screen(COLS, ROWS)
    stream = pyte.Stream(screen)
    p = PtyProcess.spawn(["node", "-e", script], dimensions=(ROWS, COLS), env=env, cwd=ROOT)

    stop = threading.Event()
    raw_acc = []

    def reader():
        while not stop.is_set():
            try:
                raw = p.read(65536)
            except Exception:
                return
            if not raw:
                time.sleep(0.01)
                continue
            raw_acc.append(raw)
            try:
                stream.feed(raw)
            except Exception:
                pass

    t = threading.Thread(target=reader, daemon=True)
    t.start()
    time.sleep(1.2)
    stop.set()
    try:
        p.terminate(force=True)
    except Exception:
        pass

    print(f"\n[{name}]")
    print(f"  pyte cursor = ({screen.cursor.y}, {screen.cursor.x})  hidden={screen.cursor.hidden}")
    print("  屏幕:")
    for y, line in enumerate(screen.display):
        if line.strip():
            print(f"    {y:02d}|{line.rstrip()}")
    blob = "".join(raw_acc)
    print("  原始字节尾部:", repr(blob[-140:]))
    return (screen.cursor.y, screen.cursor.x)


try:
    res = {name: run_case(name, script) for name, script in CASES.items()}
    print("\n=== 结论 ===")
    for name, pos in res.items():
        print(f"  {name} → cursor={pos}")
    a = list(res.values())[0]
    b = list(res.values())[1]
    c = list(res.values())[2]
    d = list(res.values())[3]
    print(f"  期望 col=2；A(主屏绝对)={a}  B(主屏相对)={b}  C(alt相对)={c}  D(alt+BSU相对)={d}")
    if a[1] == 2 and b[1] == 2 and c[1] == 2 and d[1] == 2:
        print("  → 全部保留；光标偏移另有原因")
    elif a[1] == 2 and b[1] == 2 and (c[1] != 2 or d[1] != 2):
        print("  → alt-screen 下相对移动被重写！这是 GA fullscreen park 失效的直接证据")
    elif a[1] == 2 and b[1] != 2:
        print("  → ConPTY 重写了相对移动")
    else:
        print("  → 都不符预期，pyte 在 ConPTY 下的回放不可信")
finally:
    finish(_guard, 0)
