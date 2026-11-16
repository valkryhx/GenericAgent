# -*- coding: utf-8 -*-
"""探针：真机（真 PTY + 真 App）里 park 到底有没有发生，规格是什么。

测什么
------
打开产品自带的 `GA_UI_PARK_DEBUG=1`，从 PTY 流里抓 `[park] ...` 行
（`stdoutCursorPark.ts:238` 的 debug 输出），并同时读回 pyte 屏幕上的
**原生光标位置**与**输入框反显块位置**。

为什么要这样做
--------------
ConPTY 会改写 node 写出的 ANSI（ECH `CSI nX`、`CSI nC`、绝对 `CSI H` 都会出现在
流里，而 GA 源码里根本没有这些序列的生成点 —— 见 `raw_writes.py` 的同一观察）。
因此"看原始字节判断 park 有没有发"是不可靠的；而纯文本日志能穿过 ConPTY 保真。

⚠️ **本探针依赖的调试旁路已被移除。** 排查期间曾给 `stdoutCursorPark.ts` 加过两条
临时钩子（`GA_UI_PARK_LOG` 写 SINK 字节、`GA_UI_FRAME_DUMP` 导出整帧），修复落地后
它们被删掉了（产品代码不留调试分支）。本探针现在只能拿到屏幕侧的 pyte 读数，不再有
writer 侧的 spec 日志；需要那份数据时，参考 git 历史里 `stdoutCursorPark.ts` 的
diff 重新加回，或直接用 `_probe_park_config_matrix.py`（它只依赖屏幕读数，不需要钩子）。

⚠️ **诊断旁路必须走文件、不能走 console。** 曾用 `console.error` 打 spec，ink 的
`patchConsole` 把它写进 stdout 并重画上一帧 → 每条日志又触发一次渲染 → 13 秒刷出
8265 条、每帧退化成 1 行，读到 `spec=null` 是纯自反馈产生的假象。

根因坐标（已修复）
----------------
`stdoutCursorPark.ts` 的 park 段 `needShow = !this.cursorShown` 记账错误：ConPTY 在
BSU/ESU 边界重置 DECTCEM，导致整个会话只 SHOW 一次，原生光标停在 (39,119)。
修复 = 每帧重发 SHOW。完整分析见
`docs/20261116-GA-中文输入法光标不跟输入框根因.md`。

为什么必须保留
--------------
2026-07-16 的修复（park 后 SHOW）只覆盖"spec 非 null"的情形；2026-11-16 的修复补上
"每帧都要 SHOW"。这两个不变量（spec 非 null、SHOW 每帧都发）在真机上都曾静默失效过，
而单测全绿。保留本探针作为「真机侧观察入口」，与 `_probe_park_config_matrix.py`
（A/B/C 配置对照，权威判据）配合使用。

运行：python tests/probes/python/_probe_park_spec_real.py
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

ROOT = r"D:\git_codes\GenericAgent"
COLS, ROWS = 120, 40
IDLE = 1.8

_guard = start(ROOT, quiet=True)

env = dict(os.environ)
for k in [k for k in env if k.upper().startswith(("CLAUDE", "ANTHROPIC"))]:
    env.pop(k, None)
env["PYTHONPATH"] = ROOT
env["PYTHONIOENCODING"] = "utf-8"
env["PYTHONUTF8"] = "1"
env["TERM"] = "xterm-256color"
env.pop("GA_UI_PARK_DEBUG", None)
env["GA_UI_PARK_LOG"] = os.path.join(ROOT, "temp", "park_writer.log")
env["GA_UI_FRAME_DUMP"] = os.path.join(ROOT, "temp", "frames.jsonl")

screen = pyte.Screen(COLS, ROWS)
stream = pyte.Stream(screen)
lock = threading.Lock()
stop = threading.Event()
acc = {"park_lines": [], "show": 0, "hide": 0, "tail": ""}

p = PtyProcess.spawn(
    ["bash", "-lc", f"cd {ROOT.replace(os.sep, '/')} && ./ga"],
    dimensions=(ROWS, COLS), env=env, cwd=ROOT,
)


def reader():
    while not stop.is_set():
        try:
            raw = p.read(65536)
        except Exception:
            return
        if not raw:
            time.sleep(0.02)
            continue
        text = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
        with lock:
            acc["tail"] = (acc["tail"] + text)[-6000:]
            acc["show"] += len(re.findall(r"\x1b\[\?25h", text))
            acc["hide"] += len(re.findall(r"\x1b\[\?25l", text))
            for m in re.finditer(r"\[(?:park|park-spec|park-effect)\][^\r\n\x1b]*", text):
                acc["park_lines"].append(m.group(0))
            try:
                stream.feed(raw)
            except Exception:
                pass


threading.Thread(target=reader, daemon=True).start()


def snap():
    with lock:
        disp = list(screen.display)
        cur = (screen.cursor.y, screen.cursor.x)
        buf = {x: screen.buffer[screen.cursor.y][x] for x in range(COLS)}
        return disp, cur, dict(acc)


def composer_row(disp):
    for y in range(len(disp) - 2, -1, -1):
        if re.fullmatch(r"─{8,}", disp[y].strip()) and disp[y + 1].lstrip().startswith(">"):
            return y
    return -1


try:
    for _ in range(60):
        time.sleep(0.5)
        if composer_row(snap()[0]) >= 0:
            break
    time.sleep(1.0)

    for ch in "abcde":
        p.write(ch)
        time.sleep(0.12)
    time.sleep(IDLE)

    disp, cur, a = snap()
    top = composer_row(disp)
    row = top + 1 if top >= 0 else -1
    pyscreen = screen
    rev = [(x, pyscreen.buffer[row][x].data) for x in range(COLS)
           if row >= 0 and pyscreen.buffer[row][x].reverse]
    block = rev[0][0] if rev else None

    print(f"输入行          = {row}  内容={disp[row].rstrip()!r}" if row >= 0 else "输入行未定位")
    print(f"反显块列        = {block}  ({len(rev)} 格)")
    print(f"原生光标        = {cur}   pyte cursor.hidden={screen.cursor.hidden}")
    print(f"DECTCEM         = show {a['show']} / hide {a['hide']}")
    print(f"[park] 日志条数 = {len(a['park_lines'])}")
    for line in a["park_lines"][-6:]:
        print(f"   {line}")
    if not a["park_lines"]:
        print("   （没有 [park] 日志：本帧没走到 computeLineDiff 的可解析分支，或 diff 未开）")
    print("\n尾部 400 字节（转义可见）：")
    print(" " + repr(a["tail"][-400:]))
finally:
    stop.set()
    try:
        p.terminate(force=True)
    except Exception:
        pass
    finish(_guard, 0)
