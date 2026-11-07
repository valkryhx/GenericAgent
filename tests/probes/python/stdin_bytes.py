# -*- coding: utf-8 -*-
"""真机抓包：滚轮时 GA 进程的 stdin 到底收到什么字节。

做法：写一个极小的 node 探针，以 raw mode 读 stdin，打印收到的原始字节。
然后用 PTY 写入同一串 SGR 滚轮序列，对比。
"""
import os
import subprocess
import sys
import threading
import time

from winpty import PtyProcess

ROOT = r"D:\git_repos\GenericAgent"
COLS, ROWS = 110, 40

PROBE = r"""
process.stdin.setRawMode(true)
process.stdin.resume()
process.stdin.on('data', (buf) => {
  const s = buf.toString('binary')
  const hex = Array.from(buf).map(b => b.toString(16).padStart(2,'0')).join(' ')
  process.stdout.write('BYTES ' + hex + '  LEN ' + buf.length + '\r\n')
})
setTimeout(() => process.exit(0), 12000)
"""

env = dict(os.environ)
env["PYTHONIOENCODING"] = "utf-8"
env["PYTHONUTF8"] = "1"
env["TERM"] = "xterm-256color"

node = "node"
p = PtyProcess.spawn(
    [node, "-e", PROBE],
    dimensions=(ROWS, COLS), env=env, cwd=ROOT,
)

out = []


def reader():
    while True:
        try:
            raw = p.read(65536)
        except Exception:
            break
        if not raw:
            time.sleep(0.01)
            continue
        out.append(raw)


threading.Thread(target=reader, daemon=True).start()
time.sleep(2)

cases = [
    ("SGR wheel up  ESC[<64;10;10M", "\x1b[<64;10;10M"),
    ("SGR wheel down ESC[<65;10;10M", "\x1b[<65;10;10M"),
    ("X10 wheel up   ESC[M`!!", "\x1b[M`!!"),
    ("PgUp           ESC[5~", "\x1b[5~"),
]

for label, seq in cases:
    out.clear()
    p.write(seq)
    time.sleep(1.0)
    got = "".join(out)
    print(f"--- {label} ---")
    for line in got.splitlines():
        if line.startswith("BYTES"):
            print(f"    {line}")
    if not any(l.startswith("BYTES") for l in got.splitlines()):
        print(f"    (无 BYTES 输出) raw={got[:120]!r}")

time.sleep(9)
try:
    p.terminate(force=True)
except Exception:
    pass
