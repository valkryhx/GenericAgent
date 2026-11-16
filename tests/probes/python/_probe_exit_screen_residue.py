# -*- coding: utf-8 -*-
"""探针：`ga` 退出后，主屏上到底残留什么（用户报的「下边线短一截」）。

## 测什么

在真 PTY 里启动 `./ga`，等 TUI 渲染好、输入几个字符，然后**按 Ctrl+C 两次**
（GA 的退出语义：一次提示、二次退出），再用 pyte 回放整段输出，读回：

  1. 退出后光标停在主屏的哪一行；
  2. 主屏上是否残留旧的 composer 边框（`────`）；
  3. 退出序列里有没有 `ESC[?1049l`（回到主屏）、有没有 `ESC[2J`（清屏）。

## 为什么这么做

用户描述：退出后「光标没回到最前面，输入框下边的线短了一截」。GA 默认是
fullscreen（alt-screen，`mouseWheel.ts:112` 恒 true），退出时写
`ESC[?1049l` 让终端恢复主屏。**如果主屏本来就是干净的，用户不会看到任何残留** ——
所以要么退出序列没走到，要么 alt-screen 里的内容被写进了主屏。

ConPTY 会改写序列，因此不能只看 GA 自己的写入日志；本探针看的是**终端最终状态**。

## 判据

  - 退出后主屏无 `────` 残留、光标在主屏首行附近 → 干净
  - 有残留边框 / 光标停在半屏             → 复现用户报的问题

## 为什么必须保留

`terminalCleanup.ts` 的退出序列有多条分支（inline / fullscreen / 鼠标模式），
而单测只能断言「序列里包含某个字节」。**退出后的屏幕长什么样，只有真机回放能答**。
这是该问题唯一的判据。

运行：python tests/probes/python/_probe_exit_screen_residue.py
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

env = dict(os.environ)
for k in [k for k in env if k.upper().startswith(("CLAUDE", "ANTHROPIC"))]:
    env.pop(k, None)
env["PYTHONPATH"] = REPO
env["PYTHONIOENCODING"] = "utf-8"
env["PYTHONUTF8"] = "1"
env["TERM"] = "xterm-256color"
env.pop("GA_INK_FULLSCREEN", None)   # 用产品默认（fullscreen）

screen = pyte.Screen(COLS, ROWS)
stream = pyte.Stream(screen)
lock = threading.Lock()
stop = threading.Event()
raw_all: list[str] = []

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
        text = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
        with lock:
            raw_all.append(text)
            try:
                stream.feed(raw)
            except Exception:
                pass


threading.Thread(target=reader, daemon=True).start()


def dump(tag: str) -> None:
    with lock:
        disp = list(screen.display)
        cur = (screen.cursor.y, screen.cursor.x)
        hidden = screen.cursor.hidden
    borders = [(y, line.rstrip()[:50]) for y, line in enumerate(disp)
               if re.search(r"─{8,}", line)]
    print(f"\n[{tag}]")
    print(f"  光标 = {cur}   hidden = {hidden}")
    print(f"  含边框行 = {len(borders)}")
    for y, line in borders[:4]:
        print(f"    {y:02d}| {line}")
    nonempty = [(y, line.rstrip()[:80]) for y, line in enumerate(disp) if line.strip()]
    print(f"  非空行 = {len(nonempty)}")
    for y, line in nonempty[:6]:
        print(f"    {y:02d}| {line}")


try:
    time.sleep(9.0)          # 启动 + MCP 发现
    dump("A 启动后（TUI 运行中）")

    for ch in "abc":
        p.write(ch)
        time.sleep(0.15)
    time.sleep(1.0)
    dump("B 输入 abc 后")

    # GA 的退出语义：Ctrl+C 一次提示，两次退出
    p.write("\x03")
    time.sleep(1.2)
    dump("C 第一次 Ctrl+C 之后")

    p.write("\x03")
    time.sleep(2.5)
    dump("D 第二次 Ctrl+C 之后（应已退出）")

    blob = "".join(raw_all)
    print("\n=== 退出序列证据 ===")
    for name, seq in (
        ("回到主屏 ESC[?1049l", "\x1b[?1049l"),
        ("进备用屏 ESC[?1049h", "\x1b[?1049h"),
        ("清屏 ESC[2J", "\x1b[2J"),
        ("清 scrollback ESC[3J", "\x1b[3J"),
        ("归位 ESC[H", "\x1b[H"),
    ):
        print(f"  {name:24s} 出现 {blob.count(seq)} 次")
    print(f"\n  输出尾部 400 字符:\n  {blob[-400:]!r}")
finally:
    stop.set()
    try:
        p.terminate(force=True)
    except Exception:
        pass
    finish(_guard, 0)