# -*- coding: utf-8 -*-
"""探针：把 GA 真机上写出的**全部帧按顺序**经 ConPTY 重放，逐步读回光标。

测什么
------
`_probe_replay_real_frame.py` 只重放最后一帧就正确落在 caret (35,8)，而真机上同一
时刻光标在 (39,119)。二者唯一差别是「之前还有 12 帧」。本探针按原顺序逐帧重放，
每写完一帧就采样一次光标，从而定位「从第几帧开始光标离开 caret，以及是谁把它推走」。

判据
----
  每帧后光标都 = composer caret cell  → park 序列整体自洽，真机偏移来自 GA 之外的写入
  某帧后光标离开 caret               → 该帧的 flush 几何本身有问题（行级 diff / unpark）

为什么必须保留
--------------
单帧正确 ≠ 序列正确：行级 diff 会改变每帧实际写出的字节（例如 `changedRows=0` 时只写
一个 `\\r`），而 unpark/park 的相对位移依赖「上一帧结束时光标在哪」。这是唯一能把
「帧内几何」与「帧间记账」分开的探针。

运行：python tests/probes/python/_probe_replay_frame_sequence.py
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
    print(f"缺少 {FRAMES}")
    finish(_guard, 0)
    sys.exit(0)

frames = [json.loads(l) for l in Path(FRAMES).read_text(encoding="utf-8").splitlines() if l.strip()]
print(f"共 {len(frames)} 帧")

payload = os.path.join(ROOT, "temp", "frames_all.json")
Path(payload).write_text(json.dumps(frames), encoding="utf-8")

# 逐帧写出，每帧之间停 120ms；Python 侧按时间点采样
script = (
    "const fs=require('node:fs');"
    f"const frames=JSON.parse(fs.readFileSync({json.dumps(payload)},'utf8'));"
    "process.stdout.write('\\u001b[?1049h\\u001b[2J\\u001b[H');"
    "let i=0;"
    "const tick=()=>{"
    "  if(i>=frames.length){ setTimeout(()=>{},800); return; }"
    "  process.stdout.write(frames[i]);"
    "  process.stdout.write('\\u001b[?25h');"
    "  i++; setTimeout(tick,120);"
    "};"
    "setTimeout(tick,150);"
)

env = dict(os.environ)
for k in [k for k in env if k.upper().startswith(("CLAUDE", "ANTHROPIC"))]:
    env.pop(k, None)
env["TERM"] = "xterm-256color"

screen = pyte.Screen(COLS, ROWS)
stream = pyte.Stream(screen)
lock = threading.Lock()

p = PtyProcess.spawn(["node", "-e", script], dimensions=(ROWS, COLS), env=env, cwd=ROOT)
stop = threading.Event()


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
            try:
                stream.feed(raw)
            except Exception:
                pass


threading.Thread(target=reader, daemon=True).start()


def composer_caret():
    with lock:
        disp = list(screen.display)
        cur = (screen.cursor.y, screen.cursor.x)
        buf = {y: {x: screen.buffer[y][x] for x in range(COLS)} for y in range(ROWS)}
    for y in range(len(disp) - 2, -1, -1):
        if re.fullmatch(r"─{8,}", disp[y].strip()) and disp[y + 1].lstrip().startswith(">"):
            row = y + 1
            rev = [x for x in range(COLS) if buf[row][x].reverse]
            return row, (rev[0] if rev else None), cur
    return None, None, cur


time.sleep(0.6)
rows = []
for i in range(len(frames)):
    # 等这一帧写完（写出间隔 120ms）
    time.sleep(0.20)
    row, block, cur = composer_caret()
    parks = re.findall(r"\x1b\[(\d+)A\x1b\[(\d+)G", frames[i])
    rows.append((i + 1, len(frames[i]), parks[-1] if parks else None, row, block, cur))

stop.set()
try:
    p.terminate(force=True)
except Exception:
    pass

print(f"\n{'帧':>3} {'字节':>5} {'park段':>10} {'composer行':>10} {'反显块列':>8} {'光标':>12} 判定")
bad = 0
for (n, size, park, row, block, cur) in rows:
    if row is None:
        verdict = "未定位 composer"
    elif cur == (row, block):
        verdict = "OK"
    else:
        verdict = "<<偏离>>"
        bad += 1
    print(f"{n:>3} {size:>5} {str(park):>10} {str(row):>10} {str(block):>8} {str(cur):>12} {verdict}")

print(f"\n偏离帧数 = {bad} / {len(rows)}")
print("结论：" + ("全部帧后光标都在 caret（帧间记账自洽）" if bad == 0
                 else "存在帧把光标留在 caret 之外 —— 真机偏移在 GA 自身的帧序列内即可复现"))
finish(_guard, 0)
