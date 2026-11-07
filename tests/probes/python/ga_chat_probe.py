# -*- coding: utf-8 -*-
"""在真正的 `./ga` Ink TUI 里打一句话并回车，确认能拿到模型回复。

相比上一版：
- 后台线程持续读，主线程只管时间轴（避免 read 阻塞把整个脚本卡死）
- 抓包**增量写盘**，脚本被杀也留证据
- 所有 print 都 flush
"""
import os
import re
import sys
import threading
import time

from winpty import PtyProcess

ROOT = r"D:\git_repos\GenericAgent"
OUT = ".tmp_ga_chat.capture.txt"
PROMPT = "Reply with exactly: TUI-OK"
BOOT_WAIT = 35.0     # 等启动 + MCP 连接
REPLY_WAIT = 60.0    # 等模型回复

env = dict(os.environ)
for k in [k for k in env if k.upper().startswith(("CLAUDE", "ANTHROPIC"))]:
    env.pop(k, None)
env["PYTHONPATH"] = ROOT
env["PYTHONIOENCODING"] = "utf-8"
env["PYTHONUTF8"] = "1"

p = PtyProcess.spawn(
    ["bash", "-lc", "cd /d/git_repos/GenericAgent && ./ga"],
    dimensions=(40, 120),
    env=env,
    cwd=ROOT,
)

buf = []
lock = threading.Lock()
stop = threading.Event()
f = open(OUT, "w", encoding="utf-8", newline="\n")


def reader():
    while not stop.is_set():
        try:
            raw = p.read(65536)
        except EOFError:
            break
        except Exception:
            break
        if not raw:
            time.sleep(0.03)
            continue
        with lock:
            buf.append(raw)
        try:
            f.write(raw)
            f.flush()
        except Exception:
            pass


th = threading.Thread(target=reader, daemon=True)
th.start()

t0 = time.time()
print("waiting %.0fs for boot..." % BOOT_WAIT, flush=True)
time.sleep(BOOT_WAIT)

with lock:
    boot_len = sum(len(x) for x in buf)
print("boot chars:", boot_len, flush=True)

print("typing prompt...", flush=True)
p.write(PROMPT)
time.sleep(1.5)
p.write("\r")
print("sent, waiting %.0fs for reply..." % REPLY_WAIT, flush=True)
time.sleep(REPLY_WAIT)

with lock:
    total = sum(len(x) for x in buf)
print("total chars:", total, flush=True)

stop.set()
try:
    f.close()
except Exception:
    pass
try:
    p.terminate(force=True)
except Exception:
    pass

text = "".join(buf)


def strip_ansi(s):
    s = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", s)
    s = re.sub(r"\x1b\][^\x07]*\x07", "", s)
    return s


plain = strip_ansi(text)
print()
print("=== 判定 ===", flush=True)
print("TUI-OK 在抓包里 :", "yes" if "TUI-OK" in plain else "no", flush=True)
print("崩溃特征        :",
      "yes" if any(s in text for s in ("Traceback", "ModuleNotFoundError",
                                       "ERR_MODULE_NOT_FOUND", "Cannot find module")) else "no",
      flush=True)
print()
print("=== 末 1500 字符（可见文本）===", flush=True)
print(plain[-1500:], flush=True)
