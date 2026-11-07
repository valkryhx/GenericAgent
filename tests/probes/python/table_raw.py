# -*- coding: utf-8 -*-
"""抓 bridge 真实发出的 assistant_done 文本，看表格的原始字节。"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(r"D:\git_repos\GenericAgent")
BRIDGE = REPO / "frontends" / "ink" / "bridge.py"
PROMPT = (
    "请只输出下面这个 markdown 表格，不要任何解释文字、不要代码块围栏：\n"
    "| 名称 | 延迟 | 说明 |\n"
    "| --- | --- | --- |\n"
    "| 缓存 | 12ms | 命中率高 |\n"
    "| 回源 | 180ms | 首次请求 |\n"
)

env = dict(os.environ)
env["PYTHONIOENCODING"] = "utf-8"
env["PYTHONUNBUFFERED"] = "1"
env["PYTHONPATH"] = str(REPO) + os.pathsep + str(REPO / "frontends")

proc = subprocess.Popen(
    [sys.executable, "-u", str(BRIDGE)],
    cwd=str(REPO), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
    stderr=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
    bufsize=1, env=env,
)


def send(o):
    proc.stdin.write(json.dumps(o, ensure_ascii=False) + "\n")
    proc.stdin.flush()


done_text = None
deltas = []
deadline = time.time() + 120
while time.time() < deadline:
    line = proc.stdout.readline()
    if not line:
        break
    try:
        ev = json.loads(line.strip())
    except json.JSONDecodeError:
        continue
    t = ev.get("type")
    if t == "ready":
        send({"type": "submit", "text": PROMPT})
    elif t == "assistant_delta":
        deltas.append(ev.get("text") or "")
    elif t == "assistant_done":
        done_text = ev.get("text") or ""
    elif t == "status" and ev.get("status") == "idle" and deltas:
        break

try:
    send({"type": "shutdown"})
except Exception:
    pass
try:
    proc.wait(timeout=8)
except subprocess.TimeoutExpired:
    proc.kill()

print(f"delta 数 {len(deltas)}")
if done_text is None:
    print("没有 assistant_done")
    sys.exit(1)

print("\n=== assistant_done 原文（repr，前 800 字符）===")
print(repr(done_text[:800]))

print("\n=== 逐行 ===")
for i, l in enumerate(done_text.split("\n")[:20]):
    print(f"  {i:2d} | {l!r}")

# 直接喂给 marked 看 token
sys.path.insert(0, str(REPO / "frontends" / "ink" / "ui"))
print("\n=== 是否含 markdown 表格分隔行 ===")
import re
has_sep = any(re.match(r"^\s*\|?[\s:-]+\|[\s:|-]+$", l) for l in done_text.split("\n"))
print(f"  {has_sep}")
