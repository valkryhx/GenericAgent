# -*- coding: utf-8 -*-
"""抓真机原始写入字节，看每次 write 到底长什么样。

上一轮探针把 26 次写入全判成「无 erase 的多行」，但每次 8~30 行递增，
不像 Static 增量追加，更像「整帧全量重画」。必须先看原始字节，
确认 ESC[2K / ESC[nA 是否被 ConPTY 改写或吞掉。
"""
import os
import re
import sys
import threading
import time

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
        with lock:
            chunks.append((time.time() - session["t0"], raw))
        time.sleep(0)


th = threading.Thread(target=reader, daemon=True)
th.start()
time.sleep(30)

PROMPT = "请用中文写 2 段话介绍终端渲染。直接给正文。"
p.write(PROMPT)
time.sleep(0.8)
p.write("\r")
session["t0"] = time.time()
time.sleep(25)
stop.set()
time.sleep(0.3)
try:
    p.terminate(force=True)
except Exception:
    pass

with lock:
    got = list(chunks)

print(f"抓到 {len(got)} 个写入块\n")

CSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]")

for i, (t, raw) in enumerate(got[:8]):
    text = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
    seqs = CSI_RE.findall(text)
    from collections import Counter
    cnt = Counter(seqs)
    print(f"--- #{i} t={t:.3f}s  {len(text)} chars ---")
    print(f"  CSI 序列 top: {cnt.most_common(6)}")
    print(f"  首 160 字符 repr: {text[:160]!r}")
    print()

# 全量统计 CSI 种类
allseq = Counter()
for _, raw in got:
    text = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
    allseq.update(CSI_RE.findall(text))
print("=== 全部写入的 CSI 序列统计 ===")
for s, c in allseq.most_common(20):
    print(f"  {s!r:24} x{c}")
