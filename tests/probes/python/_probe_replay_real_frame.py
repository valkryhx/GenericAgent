# -*- coding: utf-8 -*-
"""探针：把 GA 在真机上写出的**完整一帧**经 ConPTY 重放，看光标落在哪里。

测什么
------
从 `GA_UI_FRAME_DUMP` 导出的帧（jsonl，每行一个 JSON 字符串）里取最后一帧，
原样通过 node 写到 PTY，再用 pyte 回放 ConPTY 的输出，读回光标绝对位置，并与
该帧 park 段声明的 up/col 推出的期望位置比对。

为什么必须这样做
----------------
`_probe_park_spec_real.py` 已同时拿到两个看似矛盾的事实：
  - GA 发出的最后一条写入末尾是 `ESC[4A ESC[9G`（park 段存在且正确）；
  - 同一时刻真 PTY 上 pyte 读到的光标在 (39,119)（屏幕右下角）。

`_probe_conpty_cursor_control.py` 的 A/B/C/D 对照证明「相对移动在 ConPTY 上会被
保留」。所以矛盾只能出在**这一帧本身的字节**上：真实的帧比探针手写的最小样例复杂
得多（含 `ESC[nK`、`ESC[nX`、`ESC[nC`、BSU/ESU、`\r\n`），其中某个组合会让光标在
park 之后又被推到行尾。

本探针把这一帧单独重放，因此排除了「后续还有别的写入」这个混淆项 —— 若单帧重放
后光标仍不在 caret，那么问题就在这一帧内部，与 App/ink 的后续渲染无关。

为什么必须保留
--------------
这是唯一用产品真实字节（而非手写样例）验证 park 语义的探针。手写样例永远测不出
「整帧里某些序列相互抵消」这类缺陷，而本仓库历史上多次栽在「探针复算产品公式」
的假通过上（见 AGENTS.md）。

运行：python tests/probes/python/_probe_replay_real_frame.py
前提：先跑 `_probe_park_spec_real.py` 生成 temp/frames.jsonl
"""
import json
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
FRAMES = os.path.join(ROOT, "temp", "frames.jsonl")
COLS, ROWS = 120, 40

_guard = start(ROOT, quiet=True)

if not os.path.exists(FRAMES):
    print(f"缺少 {FRAMES}：先跑 _probe_park_spec_real.py")
    finish(_guard, 0)
    sys.exit(0)

lines = [json.loads(l) for l in Path(FRAMES).read_text(encoding="utf-8").splitlines() if l.strip()]
frame = lines[-1]
print(f"取最后一帧：{len(frame)} 字符")

# 从帧里解析 park 段：最后的 ESC[nA ESC[mG
parks = re.findall(r"\x1b\[(\d+)A\x1b\[(\d+)G", frame)
print("帧内 park 段:", parks[-3:] if parks else "无")

# 用 node 原样写出这一帧（不经 JSON 转义损失）
script = (
    "const fs=require('node:fs');"
    f"const b=JSON.parse(fs.readFileSync({json.dumps(os.path.join(ROOT,'temp','frame_one.json'))},'utf8'));"
    "process.stdout.write('\\u001b[?1049h\\u001b[2J\\u001b[H');"
    "process.stdout.write(b);"
    "process.stdout.write('\\u001b[?25h');"
    "setTimeout(()=>{},600)"
)
Path(os.path.join(ROOT, "temp", "frame_one.json")).write_text(json.dumps(frame), encoding="utf-8")

env = dict(os.environ)
for k in [k for k in env if k.upper().startswith(("CLAUDE", "ANTHROPIC"))]:
    env.pop(k, None)
env["TERM"] = "xterm-256color"

screen = pyte.Screen(COLS, ROWS)
stream = pyte.Stream(screen)
raw_acc = []

p = PtyProcess.spawn(["node", "-e", script], dimensions=(ROWS, COLS), env=env, cwd=ROOT)
stop = threading.Event()


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


threading.Thread(target=reader, daemon=True).start()
time.sleep(2.0)
stop.set()
try:
    p.terminate(force=True)
except Exception:
    pass

print(f"\n重放后 pyte cursor = ({screen.cursor.y}, {screen.cursor.x})  hidden={screen.cursor.hidden}")
disp = screen.display
composer = -1
for y in range(len(disp) - 2, -1, -1):
    if re.fullmatch(r"─{8,}", disp[y].strip()) and disp[y + 1].lstrip().startswith(">"):
        composer = y + 1
        break
print(f"composer 输入行 = {composer}")
if composer >= 0:
    rev = [x for x in range(COLS) if screen.buffer[composer][x].reverse]
    print(f"反显块列        = {rev[0] if rev else None}")
print("\n屏幕（★=光标行）:")
for y, line in enumerate(disp):
    if line.strip():
        mark = "★" if y == screen.cursor.y else " "
        print(f"  {mark}{y:02d}|{line.rstrip()[:112]}")

print("\nConPTY 输出尾部:", repr("".join(raw_acc)[-300:]))
finish(_guard, 0)
