# -*- coding: utf-8 -*-
"""验证 ?2026h / ?2026l 在真机上是否被终端接受（不是被 ConPTY 吞掉）。

之前的负对照结论是：ConPTY 会【重排】嵌套、且抓到的是"空对"。
但那次是**一次 write 写全部**。这次模拟 Ink 的真实模式：
每次 write 是独立的「擦除+重写」，我们要看 ConPTY 是否仍保留成对。
"""
import os
import sys
import time

from winpty import PtyProcess

CHILD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "_child_sync_frames.py")

if not os.path.exists(CHILD):
    with open(CHILD, "w", encoding="utf-8", newline="\n") as f:
        f.write('''# -*- coding: utf-8 -*-
"""子进程：模拟 Ink log-update 的 3 帧，每帧包在 BSU/ESU 里。"""
import sys, time

BSU = "\\x1b[?2026h"
ESU = "\\x1b[?2026l"
ERASE = "\\x1b[2K"

for frame in range(3):
    body = (ERASE + "FRAME-%d-LINE-A\\r\\n" % frame) * 3
    sys.stdout.write(BSU + body + ESU)
    sys.stdout.flush()
    time.sleep(0.15)

sys.stdout.write("ALLDONE\\r\\n")
sys.stdout.flush()
time.sleep(0.5)
''')
    print("created child script")

p = PtyProcess.spawn([sys.executable, CHILD], dimensions=(30, 100))
out = ""
t0 = time.time()
try:
    while time.time() - t0 < 8:
        chunk = p.read(65536)
        if chunk:
            out += chunk
            if "ALLDONE" in out:
                break
        else:
            time.sleep(0.02)
except Exception as e:
    print("read err", e)
try:
    p.terminate(force=True)
except Exception:
    pass

bsu = out.count("\x1b[?2026h")
esu = out.count("\x1b[?2026l")
print("BSU:", bsu, " ESU:", esu)
print("配对:", "YES" if bsu == esu and bsu > 0 else "NO")

# 检查每个 BSU..ESU 之间是否真的有 FRAME 内容
import re
pairs = re.findall(r"\x1b\[\?2026h(.*?)\x1b\[\?2026l", out, re.S)
print("完整区间数:", len(pairs))
with_content = sum(1 for s in pairs if "FRAME" in s)
print("区间内有内容的:", with_content, "/", len(pairs))
if pairs:
    print("首个区间前 60 字符:", repr(pairs[0][:60]))
print()
print("VERDICT:", "同步输出可用" if with_content > 0 else "被 ConPTY 破坏（空对）")
