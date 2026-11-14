# -*- coding: utf-8 -*-
"""用 PTY 启动真正的 `./ga`（Ink TUI），确认它渲染出界面而不是崩掉。

用法：python ga_tui_probe.py <秒数>
产出：tests/probes/captures/ga_tui.capture.txt（原始抓包）+ 控制台摘要

护栏：开头 `start()` 快照工作树，结尾 `finish()` 校验。真 TUI 会跑真模型，
模型有可能顺手改源码（2026-11-14 真实发生过），探针期间必须守住。
"""
import os
import re
import sys
import time
from pathlib import Path

from winpty import PtyProcess

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _workspace_guard import finish, start  # noqa: E402

ROOT = r"D:\git_repos\GenericAgent"
SECONDS = float(sys.argv[1]) if len(sys.argv) > 1 else 25.0

_guard = start(ROOT)

env = dict(os.environ)
# 清掉会干扰的嵌套标记
for k in [k for k in env if k.upper().startswith(("CLAUDE", "ANTHROPIC"))]:
    env.pop(k, None)
# 关键：源码目录进 sys.path，因为 ga_cli 会 chdir 到 frontends/ink/ui
env["PYTHONPATH"] = ROOT
env["PYTHONIOENCODING"] = "utf-8"
env["PYTHONUTF8"] = "1"

p = PtyProcess.spawn(
    ["bash", "-lc", "cd /d/git_repos/GenericAgent && ./ga"],
    dimensions=(40, 120),
    env=env,
    cwd=ROOT,
)

chunks = []
t0 = time.time()
deadline = t0 + SECONDS
try:
    while time.time() < deadline:
        try:
            raw = p.read(65536)
        except EOFError:
            chunks.append((time.time() - t0, "<EOF>"))
            break
        if raw:
            chunks.append((time.time() - t0, raw))
        else:
            time.sleep(0.03)
finally:
    try:
        p.terminate(force=True)
    except Exception:
        pass

text = "".join(c for _, c in chunks if c != "<EOF>")
with open(".tmp_ga_tui.capture.txt", "w", encoding="utf-8", newline="\n") as f:
    f.write(text)

print("chunks      :", len(chunks))
print("chars       :", len(text))
print("first chunk : %.2fs" % chunks[0][0] if chunks else "n/a")
print("last chunk  : %.2fs" % chunks[-1][0] if chunks else "n/a")

# 判断"渲染成功"vs"崩溃"的硬指标
plain = re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", text)
plain = re.sub(r"\x1b\][^\x07]*\x07", "", plain)
print()
print("--- 崩溃特征 ---")
for sig in ("Traceback", "ModuleNotFoundError", "Cannot find module",
            "ERR_MODULE_NOT_FOUND", "command not found"):
    if sig in text:
        print("  !! FOUND:", sig)
print("--- 渲染特征 ---")
for sig in ("?2026h", "?1049h", "GenericAgent", "ga ", ">"):
    print("  %-16s %s" % (sig, "yes" if sig in text else "no"))

print()
print("--- 可见文本首 800 字符 ---")
print(plain[:800])

finish(_guard)
