# -*- coding: utf-8 -*-
"""探针：ConPTY 是否在 ESU 处丢弃「事务内的光标移动」——加大 payload 后的复测。

⚠️ **本探针的期望值算错了，结论作废，保留作反面教材。**

`CARET_UP` 按 `len(LINES) - 25` 计算、期望行写成 24，但帧内容实际从屏幕第 1 行开始
（`CSI H` 归位后先写第一行），caret 在 0-based 第 25 行。因此 A 组（无事务的基线）
报 NG，而基线必须正确才有对照意义 —— 这一版的 NG 是探针自己的错，不是产品的。

真正确认根因的是 `_probe_park_config_matrix.py`：A 默认偏离、B 关同步输出正确、
C 关 park 偏离。随后的隔离实验表明 BSU/ESU **不**破坏相对光标移动；B 之所以正确，
是因为关掉同步输出让代码走「每帧 HIDE→SHOW」分支，SHOW 因此被每帧发出。

完整链路见 `docs/20261116-GA-中文输入法光标不跟输入框根因.md` §3。

以下是原始说明（保留）：

测什么
------
`_probe_sync_output_cursor_conflict.py` 用 3 行短内容测出「BSU/ESU 不冲突」，但那次
ConPTY 恰好把空的 `BSU ESU` 单独发了出来、内容和移动都落在事务之外，所以那次
「通过」是**假通过**（探针没造出真实的失败条件）。

本探针把 payload 做大到与 GA 真实帧同量级（含 `ESC[2K` 擦除、`\\r\\n`、80+ 列长行、
尾随一段假 MCP 横幅），再测三种形态：

  A 裸帧 + 相对 park + SHOW                       （无 BSU/ESU，基线）
  B BSU + 裸帧 + 相对 park + ESU + SHOW           （GA 现在的形态）
  C BSU + 裸帧 + ESU + 相对 park + SHOW           （park 移出事务的候选修法）

判据（期望停在 caret 行、caret 列）
----------------------------------
  A 对 / B 错 / C 对 → 确认 ConPTY 在 ESU 处丢弃事务内移动；修法 = park 移出事务
  A/B/C 全对         → 真机偏移另有原因
  A 对 / B 错 / C 错 → 修法无效，需要换机制（例如事务外绝对定位）

为什么必须保留
--------------
这是唯一在「真实帧量级」下判定 BSU/ESU 与相对光标移动是否兼容的探针。前一个版本
因为 payload 太小而产生假通过，正是本仓库反复踩到的「探针没造出失败条件」形状
（AGENTS.md 硬规则 2）。保留它，同时保留那份失败记录。

运行：python tests/probes/python/_probe_conpty_esu_cursor_drop.py
"""
import os
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

_guard = start(ROOT, quiet=True)

# 造一段与 GA 真实帧同量级的内容：20 行，每行带 ESC[2K 擦除 + \r\n + 长文本
LINES = []
for i in range(28):
    LINES.append("\x1b[2K" + (" " * 3) + f"row-{i:02d} " + ("x" * 90))
BODY = "\r\n".join(LINES) + "\r\n"
# caret 在 BODY 的第 25 行（0-based 24），列 8（0-based）
CARET_UP = len(LINES) - 25   # 从帧底上移的行数
CARET_COL = 9                # 1-based

BSU = "\x1b[?2026h"
ESU = "\x1b[?2026l"
MOVE = f"\x1b[{CARET_UP}A\x1b[{CARET_COL}G"
SHOW = "\x1b[?25h"

CASES = {
    "A 裸帧 + park + SHOW": BODY + MOVE + SHOW,
    "A2 裸帧 + park（无 SHOW）": BODY + MOVE,
    "B 事务包住帧+park + SHOW": BSU + BODY + MOVE + ESU + SHOW,
    "B2 事务包住帧+park（无 SHOW，GA 稳定态）": BSU + BODY + MOVE + ESU,
    "C park 移出事务 + SHOW": BSU + BODY + ESU + MOVE + SHOW,
}


def run(name, payload):
    env = dict(os.environ)
    for k in [k for k in env if k.upper().startswith(("CLAUDE", "ANTHROPIC"))]:
        env.pop(k, None)
    env["TERM"] = "xterm-256color"

    # 用文件传 payload，避免命令行转义
    import json
    path = os.path.join(ROOT, "temp", "esu_case.json")
    Path(path).write_text(json.dumps(payload), encoding="utf-8")
    script = (
        "const fs=require('node:fs');"
        f"const p=JSON.parse(fs.readFileSync({json.dumps(path)},'utf8'));"
        "process.stdout.write('\\u001b[?1049h\\u001b[2J\\u001b[H');"
        "process.stdout.write(p);"
        "setTimeout(()=>{},700)"
    )

    screen = pyte.Screen(COLS, ROWS)
    stream = pyte.Stream(screen)
    stop = threading.Event()
    raw_acc = []

    p = PtyProcess.spawn(["node", "-e", script], dimensions=(ROWS, COLS), env=env, cwd=ROOT)

    def reader():
        while not stop.is_set():
            try:
                raw = p.read(65536)
            except Exception:
                return
            if not raw:
                time.sleep(0.01)
                continue
            raw_acc.append(raw)
            try:
                stream.feed(raw)
            except Exception:
                pass

    threading.Thread(target=reader, daemon=True).start()
    time.sleep(2.0)
    stop.set()
    try:
        p.terminate(force=True)
    except Exception:
        pass

    cur = (screen.cursor.y, screen.cursor.x)
    disp = list(screen.display)
    # 该帧写入前屏幕被清空，帧占 28 行，从第 0 行开始 → caret 行索引 = 24
    expect = (24, CARET_COL - 1)
    print(f"\n[{name}]")
    print(f"  cursor={cur}  期望={expect}  {'OK' if cur == expect else 'NG'}")
    print(f"  该行内容 = {disp[24].rstrip()[:60]!r}" if len(disp) > 24 else "")
    print("  输出尾部:", repr("".join(raw_acc)[-200:]))
    return cur, expect


try:
    res = {}
    for name, payload in CASES.items():
        res[name] = run(name, payload)
    print("\n=== 结论 ===")
    for name, (cur, exp) in res.items():
        print(f"  {'OK ' if cur == exp else 'NG '} {name} → cursor={cur} 期望={exp}")
    a_ok = res["A 裸帧 + park + SHOW"][0] == res["A 裸帧 + park + SHOW"][1]
    a2_ok = res["A2 裸帧 + park（无 SHOW）"][0] == res["A2 裸帧 + park（无 SHOW）"][1]
    b_ok = res["B 事务包住帧+park + SHOW"][0] == res["B 事务包住帧+park + SHOW"][1]
    b2_ok = res["B2 事务包住帧+park（无 SHOW，GA 稳定态）"][0] == res["B2 事务包住帧+park（无 SHOW，GA 稳定态）"][1]
    c_ok = res["C park 移出事务 + SHOW"][0] == res["C park 移出事务 + SHOW"][1]
    print()
    print(f"  A={a_ok} A2={a2_ok} B={b_ok} B2={b2_ok} C={c_ok}")
    if a_ok and a2_ok and not b_ok:
        print("  → 判定确认：ConPTY 在 ESU 处丢弃事务内的光标移动（带不带 SHOW 都一样）。")
        print("    修法：把 park 段移出 BSU/ESU 事务。" + ("" if c_ok else " 但 C 仍不符预期，需改用绝对定位。"))
    elif a_ok and a2_ok and b_ok and b2_ok and c_ok:
        print("  → 判定：真实量级下三者都正确，真机偏移另有原因。")
    else:
        print("  → 判定：结果与预期不符，需复查（A/A2 必须正确才有对照意义）。")
finally:
    finish(_guard, 0)
