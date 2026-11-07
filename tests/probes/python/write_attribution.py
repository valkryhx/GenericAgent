# -*- coding: utf-8 -*-
"""决定性测量：真机一轮里，每一次 write 到底在干什么。

Ink 的写入可以分成两类：
  1. Static 追加：直接写到终端，行数会推着画面往下走（或触发终端滚动）
  2. 帧重画：先 eraseLines(n) 擦掉上 n 行，再重写 n 行

用户感到「上下位移」，只可能来自第 1 类（净增行），或第 2 类里
erase 行数 != 重写行数（净增/净减）。

本探针在第 1 类/第 2 类上分别记账，并记录输入框所在行号的时间线。

判定：
  - 若「净增行」几乎全部来自 Static，则抖动源 = Static 提交时机
  - 若来自帧重画，则抖动源 = 帧高度变化（live 槽位）
  - 若两者都很小，则位移来自终端自身滚动（wrapline / resize）
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
NL = "\n"

env = dict(os.environ)
for k in [k for k in env if k.upper().startswith(("CLAUDE", "ANTHROPIC"))]:
    env.pop(k, None)
env["PYTHONPATH"] = ROOT
env["PYTHONIOENCODING"] = "utf-8"
env["PYTHONUTF8"] = "1"
env["TERM"] = "xterm-256color"

screen = pyte.Screen(COLS, ROWS)
stream = pyte.Stream(screen)

# 统计
records = []          # 每次 read 的记账
lock = threading.Lock()
stop = threading.Event()

ERASE_RE = re.compile(r"\x1b\[2K")
ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")
OSC_RE = re.compile(r"\x1b\][^\x07]*(?:\x07|\x1b\\)")

session = {"t0": None}


def analyze(raw: str) -> dict:
    """把一次 read 拆成「帧重画」和「Static 追加」。"""
    erases = len(ERASE_RE.findall(raw))
    text = OSC_RE.sub("", raw)
    text = ANSI_RE.sub("", text)
    newlines = text.count("\n")
    # 帧重画：有 erase 的多行写入。Ink 每帧先擦 n 行，再写 n 行（换行数 ≈ n）
    # Static 追加：没有 erase 的多行写入
    if erases > 0:
        kind = "frame"
        net = newlines - erases   # 帧净增（通常 0；>0 说明帧变高了）
    elif newlines > 0:
        kind = "static"
        net = newlines
    else:
        kind = "other"
        net = 0
    return {
        "kind": kind, "erases": erases, "newlines": newlines, "net": net,
        "chars": len(raw), "bytes": len(raw.encode("utf-8", "replace")),
    }


def composer_row() -> int | None:
    disp = screen.display
    for y in range(len(disp) - 1, -1, -1):
        if re.fullmatch(r"─{8,}", disp[y].strip()):
            return y
    return None


def reader():
    while not stop.is_set():
        try:
            raw = p.read(65536)
        except Exception:
            break
        if not raw:
            time.sleep(0.005)
            continue
        try:
            stream.feed(raw)
        except Exception:
            pass
        if session["t0"] is None:
            continue
        try:
            text = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
        except Exception:
            continue
        rec = analyze(text)
        rec["t"] = time.time() - session["t0"]
        with lock:
            rec["row"] = composer_row()
            records.append(rec)


p = PtyProcess.spawn(
    ["bash", "-lc", "cd /d/git_repos/GenericAgent && ./ga"],
    dimensions=(ROWS, COLS), env=env, cwd=ROOT,
)
th = threading.Thread(target=reader, daemon=True)
th.start()

time.sleep(30)          # 等启动稳定
with lock:
    boot_records = len(records)

PROMPT = "请用中文写 3 段话介绍终端渲染，每段 2 句。直接给正文。"
p.write(PROMPT)
time.sleep(0.8)
p.write("\r")

session["t0"] = time.time()
rows_before = composer_row()

time.sleep(40)
stop.set()
time.sleep(0.3)
try:
    p.terminate(force=True)
except Exception:
    pass

with lock:
    recs = records[boot_records:]
    boot = records[:boot_records]

print(f"启动期写入 {len(boot)} 次；本轮写入 {len(recs)} 次")
if not recs:
    print("FAIL: 本轮没有抓到写入")
    sys.exit(1)

frames = [r for r in recs if r["kind"] == "frame"]
statics = [r for r in recs if r["kind"] == "static"]
others = [r for r in recs if r["kind"] == "other"]

print()
print("=== 写入分类 ===")
print(f"  帧重画 {len(frames):3d} 次   Static 追加 {len(statics):3d} 次   其他 {len(others):3d} 次")
print()
if frames:
    fs = [r["erases"] for r in frames]
    fnet = [r["net"] for r in frames]
    print("=== 帧重画：擦除行数（= 上一帧高度）===")
    print(f"  min={min(fs)} max={max(fs)}  出现的不同高度: {sorted(set(fs))}")
    print(f"  帧净增行（换行-擦除）: min={min(fnet)} max={max(fnet)} "
          f"非零次数={sum(1 for n in fnet if n != 0)}")
    print()
if statics:
    ss = [r["newlines"] for r in statics]
    print("=== Static 追加：每次行数 ===")
    print(f"  min={min(ss)} max={max(ss)} 合计={sum(ss)} 行")
    from collections import Counter
    print(f"  分布: {dict(sorted(Counter(ss).items()))}")
    print()

# 净增行来源
net_frame = sum(r["net"] for r in frames)
net_static = sum(r["newlines"] for r in statics)
print("=== 净增行来源 ===")
print(f"  帧重画净增 {net_frame} 行")
print(f"  Static 追加净增 {net_static} 行")
total = net_frame + net_static
if total:
    print(f"  => 帧占 {net_frame / total:.0%}，Static 占 {net_static / total:.0%}")
    if net_static > net_frame * 2:
        print("  => 抖动主因【Static 提交】")
    elif net_frame > net_static * 2:
        print("  => 抖动主因【帧高度变化 / live 槽位】")
    else:
        print("  => 两者相当")

# 输入框行号时间线
print()
print("=== 输入框行号（真机）===")
rows = [r["row"] for r in recs if r.get("row") is not None]
if rows:
    print(f"  起始 {rows_before} -> 范围 {min(rows)}~{max(rows)}")
    moves = []
    prev = rows_before
    for r in recs:
        cur = r.get("row")
        if cur is None or cur == prev:
            continue
        moves.append((round(r["t"], 2), prev, cur, cur - prev, r["kind"]))
        prev = cur
    print(f"  移动 {len(moves)} 次:")
    for t, a, b, d, k in moves:
        print(f"    [{t:6.2f}s] {a:2d} -> {b:2d}  ({d:+d})  触发于 {k}")
    big = [m for m in moves if abs(m[3]) >= 2]
    print(f"  其中 |Δ|>=2 的 {len(big)} 次")
else:
    print("  未识别到边框行")
