# -*- coding: utf-8 -*-
"""最直接的测量：真机一轮里，Ink 每一帧到底占几行。

从原始字节看，这一版 Ink 用 `ESC[3;1H` 绝对定位重画，每帧一个 write。
所以「帧高」可以从 write 里数出来：
  - 帧起点 = 定位序列 ESC[<row>;1H 或 ESC[H
  - 帧高   = 该 write 内 \\r\\n 的个数 + 1（到最后一个可见行为止）

同时记录每帧的锚点行号（ESC[<row>;1H 里的 row）。
若锚点行号变化，就是「整帧上下平移」的直接证据。

这与之前的「输入框行号」不同：输入框行号受边框识别影响，
锚点行号是 Ink 自己写出的，最接近真相。
"""
import os
import re
import sys
import threading
import time
from collections import Counter

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

# ESC[<r>;<c>H 或 ESC[H
CUP_RE = re.compile(r"\x1b\[(\d+);1H")
HOME_RE = re.compile(r"\x1b\[H")

chunks = []
lock = threading.Lock()
stop = threading.Event()
session = {"t0": None}

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
            time.sleep(0.005)
            continue
        if session["t0"] is None:
            continue
        text = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
        with lock:
            chunks.append((time.time() - session["t0"], text))
        time.sleep(0)


th = threading.Thread(target=reader, daemon=True)
th.start()
time.sleep(30)

PROMPT = "请用中文写 3 段话介绍终端渲染，每段 2 句。直接给正文，不要用工具。"
p.write(PROMPT)
time.sleep(0.8)
p.write("\r")
session["t0"] = time.time()
time.sleep(45)
stop.set()
time.sleep(0.3)
try:
    p.terminate(force=True)
except Exception:
    pass

with lock:
    got = list(chunks)

frames = []
for t, text in got:
    anchors = CUP_RE.findall(text)
    if HOME_RE.search(text):
        anchors.append("1")
    if not anchors:
        continue
    # 一个 write 里可能有多次定位（多次重画），按锚点切分
    parts = CUP_RE.split(text)
    # 简化：用行数最多的那段作为该 write 的主帧
    nl = text.count("\r\n") + text.count("\n")
    frames.append({"t": t, "anchor": int(anchors[0]), "rows": nl + 1, "anchors": anchors})

print(f"本轮写入 {len(got)} 块，其中含定位序列的帧 {len(frames)} 个\n")
if not frames:
    print("FAIL: 没抓到帧")
    sys.exit(1)

anchor_seq = [f["anchor"] for f in frames]
row_seq = [f["rows"] for f in frames]

print("=== 锚点行号（Ink 写出的 ESC[<row>;1H）===")
print(f"  范围 {min(anchor_seq)}~{max(anchor_seq)}  出现过的值: {sorted(set(anchor_seq))}")
changes = [(f["t"], anchor_seq[i], anchor_seq[i + 1])
           for i, f in enumerate(frames[:-1]) if anchor_seq[i] != anchor_seq[i + 1]]
print(f"  变化 {len(changes)} 次")
for t, a, b in changes[:20]:
    print(f"    [{t:6.2f}s] {a} -> {b}  ({b - a:+d})")

print()
print("=== 帧高（每次写入的行数）===")
print(f"  范围 {min(row_seq)}~{max(row_seq)}")
print(f"  分布: {dict(sorted(Counter(row_seq).items()))}")
rj = [(frames[i]["t"], row_seq[i], row_seq[i + 1], row_seq[i + 1] - row_seq[i])
      for i in range(len(frames) - 1) if row_seq[i + 1] != row_seq[i]]
print(f"  帧高变化 {len(rj)} 次，|Δ|>=2 的 {sum(1 for x in rj if abs(x[3]) >= 2)} 次")
for t, a, b, d in rj[:20]:
    print(f"    [{t:6.2f}s] {a} -> {b}  ({d:+d})")
