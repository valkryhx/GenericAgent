# -*- coding: utf-8 -*-
"""探针：`/quit` 与 Ctrl+C 两条退出路径，主屏残留是否不同。

## 测什么

同一台机器、同一终端，主屏先 echo 两行「用户已有内容」，然后分别用
`/quit` 和 Ctrl+C×2 退出，比较最终主屏：

  - 用户原有的 `PRE-EXISTING` 行还在不在；
  - composer 边框（`────`）有没有残留；
  - 光标停在哪；
  - 退出序列里 `ESC[?1049l` / `ESC[2J` / `ESC[3J` 各出现几次。

## 为什么需要它

用户实测反馈「0.6.6 打 /quit 退出时历史绘画显示也清平了，现在挺好的」。而我此前
的结论是「没有加清屏」。这两句话只有在**两条退出路径不同**时才同时成立：

  - `/quit` 会 dispatch 一条 `local_command_input`/`local_command_output`，
    走的是「先写静态历史、再退出」的路径；
  - Ctrl+C 直接 `exitCleanly()`。

本探针把两条路径分开测量，避免再凭推断下结论。

## 判据

  两条路径的「PRE-EXISTING 残留数」不同 → 说明行为确实分叉，需要分别对待
  两者相同                              → 用户感知到的差异来自别的因素

运行：python tests/probes/python/_probe_exit_paths_compare.py
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
COLS, ROWS = 120, 40

_guard = start(REPO, quiet=True)


def run_case(name: str, exit_keys: list[str]) -> dict:
    env = dict(os.environ)
    for k in [k for k in env if k.upper().startswith(("CLAUDE", "ANTHROPIC"))]:
        env.pop(k, None)
    env["PYTHONPATH"] = REPO
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["TERM"] = "xterm-256color"
    env.pop("GA_INK_FULLSCREEN", None)

    screen = pyte.Screen(COLS, ROWS)
    stream = pyte.Stream(screen)
    lock = threading.Lock()
    stop = threading.Event()
    raw: list[str] = []

    # 主屏先写两行用户内容，再启动 ga —— 这两行应当在退出后保留
    cmd = (f'cd {REPO_POSIX} && echo "PRE-EXISTING-LINE-A" '
           f'&& echo "PRE-EXISTING-LINE-B" && ./ga')
    p = PtyProcess.spawn(["bash", "-lc", cmd], dimensions=(ROWS, COLS), env=env, cwd=REPO)

    def reader():
        while not stop.is_set():
            try:
                r = p.read(65536)
            except Exception:
                return
            if not r:
                time.sleep(0.01)
                continue
            with lock:
                raw.append(r if isinstance(r, str) else r.decode("utf-8", "replace"))
                try:
                    stream.feed(r)
                except Exception:
                    pass

    threading.Thread(target=reader, daemon=True).start()

    try:
        time.sleep(9.0)
        for keys in exit_keys:
            if keys == "<CTRL-C>":
                p.write("\x03")
                time.sleep(1.2)
            else:
                for ch in keys:
                    p.write(ch)
                    time.sleep(0.08)
        time.sleep(3.0)

        with lock:
            disp = [line.rstrip() for line in screen.display]
            cur = (screen.cursor.y, screen.cursor.x)
        blob = "".join(raw)

        pre = [l for l in disp if "PRE-EXISTING" in l]
        borders = [l for l in disp if re.search(r"─{8,}", l)]
        nonempty = [l for l in disp if l.strip()]
        return {
            "name": name,
            "pre": len(pre),
            "borders": len(borders),
            "nonempty": len(nonempty),
            "cursor": cur,
            "1049l": blob.count("\x1b[?1049l"),
            "2J": blob.count("\x1b[2J"),
            "3J": blob.count("\x1b[3J"),
            "tail": blob[-260:],
        }
    finally:
        stop.set()
        try:
            p.terminate(force=True)
        except Exception:
            pass
        time.sleep(0.4)


try:
    cases = [
        ("A /quit", ["/quit", "\r"]),
        ("B Ctrl+C x2", ["<CTRL-C>", "<CTRL-C>"]),
    ]
    results = [run_case(n, k) for n, k in cases]

    print(f"\n{'路径':<14} {'用户行':>6} {'边框':>5} {'非空':>5} {'光标':>12} {'1049l':>6} {'2J':>4} {'3J':>4}")
    for r in results:
        print(f"{r['name']:<14} {r['pre']:>6} {r['borders']:>5} {r['nonempty']:>5} "
              f"{str(r['cursor']):>12} {r['1049l']:>6} {r['2J']:>4} {r['3J']:>4}")

    same = results[0]["pre"] == results[1]["pre"] and results[0]["borders"] == results[1]["borders"]
    print()
    if same:
        print("  → 两条退出路径的主屏残留一致")
    else:
        print("  → 两条退出路径**不同**（这正是用户感知差异的来源）")

    for r in results:
        print(f"\n[{r['name']}] 尾部 260 字符:\n  {r['tail']!r}")
finally:
    finish(_guard, 0)