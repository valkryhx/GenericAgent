# -*- coding: utf-8 -*-
"""验证：Ink 的一帧在 ConPTY 下被拆成几次可见更新？

判据不用字节，用「屏幕状态变化次数」：真机上用户看到的是屏幕刷新，
如果一帧内部有 N 次屏幕变化，用户就看到 N 次闪。
"""
import os
import sys
import threading
import time

import pyte
from winpty import PtyProcess

ROOT = r"D:\git_repos\GenericAgent"

env = dict(os.environ)
for k in [k for k in env if k.upper().startswith(("CLAUDE", "ANTHROPIC"))]:
    env.pop(k, None)
env["PYTHONPATH"] = ROOT
env["PYTHONIOENCODING"] = "utf-8"
env["PYTHONUTF8"] = "1"
env["TERM"] = "xterm-256color"

screen = pyte.Screen(120, 40)
stream = pyte.Stream(screen)
p = PtyProcess.spawn(
    ["bash", "-lc", "cd /d/git_repos/GenericAgent && ./ga"],
    dimensions=(40, 120), env=env, cwd=ROOT,
)
stop = threading.Event()
lock = threading.Lock()

# 记录屏幕指纹变化序列（每变一次 = 用户看到的一次刷新）
changes = []
last_fp = None


def fingerprint():
    """屏幕状态的紧凑指纹。"""
    return "\n".join(screen.display)


def reader():
    global last_fp
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
        with lock:
            fp = fingerprint()
            if fp != last_fp:
                changes.append(time.time())
                last_fp = fp


th = threading.Thread(target=reader, daemon=True)
th.start()
time.sleep(30)

changes.clear()
p.write("Count from 1 to 20, one number per line.")
time.sleep(1.0)
p.write("\r")

t0 = time.time()
time.sleep(15)
stop.set()
time.sleep(0.3)
try:
    p.terminate(force=True)
except Exception:
    pass

elapsed = time.time() - t0
n = len(changes)
print(f"屏幕刷新次数     = {n}")
print(f"统计时长         = {elapsed:.1f}s")
print(f"刷新率           = {n / elapsed:.1f} 次/秒")
if len(changes) > 1:
    gaps = [(changes[i] - changes[i - 1]) * 1000 for i in range(1, len(changes))]
    gaps.sort()
    print(f"间隔 中位数      = {gaps[len(gaps)//2]:.1f} ms")
    print(f"间隔 最小        = {gaps[0]:.1f} ms")
    print(f"间隔 最大        = {gaps[-1]:.1f} ms")
    tight = sum(1 for g in gaps if g < 5)
    print(f"<5ms 的密集刷新  = {tight} 次 ({tight/len(gaps)*100:.0f}%)")
