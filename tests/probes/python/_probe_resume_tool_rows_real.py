# -*- coding: utf-8 -*-
"""探针：真机上 `/resume` 加载的历史里，工具调用是否出现在屏幕上。

## 测什么

在真 PTY 里启动 `./ga`，执行 `/resume` 选一个有工具调用的历史会话，然后用 pyte
回放屏幕，检查：

  - 屏幕上是否出现工具 chip 形状的行（`● <tool_name>`）；
  - 是否出现结果行（`⎿`）；
  - 与「只有 prose」的情况对比。

## 为什么必须真机

`resume_tool_rows.ts` 只验证渲染函数；`test_session_transcript.py` 只验证加载层。
「这两层接上之后，屏幕上真的能看到工具调用」只有真机能回答——中间还有 bridge 的
JSONL 序列化、前端 state reducer、Ink 布局三道关。

## 根因坐标（已修）

`transcripts.py:_message_text_from_blocks` 只收 `type == "text"`；
`bridge.py:_resume_ui_messages_with_checkpoints` 只发 `assistant_text`。
两处叠加使 `/resume` 只见纯文本。

为什么必须保留：这是「历史内容完整性」唯一的端到端判据，任何一层回退都会让它变红。

运行：python tests/probes/python/_probe_resume_tool_rows_real.py
"""
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

REPO = str(Path(__file__).resolve().parents[3])
REPO_POSIX = "/" + Path(REPO).as_posix()[0].lower() + Path(REPO).as_posix()[2:]
COLS, ROWS = 130, 45

_guard = start(REPO, quiet=True)

env = dict(os.environ)
for k in [k for k in env if k.upper().startswith(("CLAUDE", "ANTHROPIC"))]:
    env.pop(k, None)
env["PYTHONPATH"] = REPO
env["PYTHONIOENCODING"] = "utf-8"
env["PYTHONUTF8"] = "1"
env["TERM"] = "xterm-256color"

screen = pyte.Screen(COLS, ROWS)
stream = pyte.Stream(screen)
lock = threading.Lock()
stop = threading.Event()

p = PtyProcess.spawn(
    ["bash", "-lc", f"cd {REPO_POSIX} && ./ga"],
    dimensions=(ROWS, COLS), env=env, cwd=REPO,
)


def reader():
    while not stop.is_set():
        try:
            raw = p.read(65536)
        except Exception:
            return
        if not raw:
            time.sleep(0.01)
            continue
        with lock:
            try:
                stream.feed(raw)
            except Exception:
                pass


threading.Thread(target=reader, daemon=True).start()


def screen_lines():
    with lock:
        return [line.rstrip() for line in screen.display]


def has_picker(lines):
    return any("resume" in line.lower() for line in lines)


try:
    time.sleep(9.0)
    print("[A] 启动完成，屏幕:")
    for line in screen_lines():
        if line.strip():
            print("   ", line[:110])

    # 打开 /resume 选择器
    for ch in "/resume":
        p.write(ch)
        time.sleep(0.06)
    time.sleep(0.4)
    p.write("\r")
    time.sleep(3.0)

    lines = screen_lines()
    print("\n[B] /resume 打开后:")
    for line in lines:
        if line.strip():
            print("   ", line[:110])

    # 选第一个（最新的）会话：Enter
    p.write("\r")
    time.sleep(4.0)

    lines = screen_lines()
    print("\n[C] 载入会话后:")
    for line in lines:
        if line.strip():
            print("   ", line[:110])

    blob = "\n".join(lines)
    chips = re.findall(r"[●⏺•]\s*([A-Za-z_][A-Za-z0-9_]*)", blob)
    results = blob.count("⎿")
    print("\n=== 判定 ===")
    print(f"  工具 chip 行 = {len(chips)}   {sorted(set(chips))[:8]}")
    print(f"  结果行 ⎿     = {results}")
    ok = len(chips) > 0
    print("  OK  resume 历史里出现了工具调用" if ok else "  NG  仍然只有纯文本")
finally:
    stop.set()
    try:
        p.terminate(force=True)
    except Exception:
        pass
    finish(_guard, 0)