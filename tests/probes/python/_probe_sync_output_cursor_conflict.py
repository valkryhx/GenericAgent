# -*- coding: utf-8 -*-
"""探针：DEC 2026（BSU/ESU）是否让「事务内的相对光标移动」在 ConPTY 上失效。

⚠️ **本探针的「通过」是假通过，结论作废，保留作反面教材。**

原因：它的 payload 只有 3 行短内容（`AAAA/BBBB/CCCC`），ConPTY 在这种量级下恰好
把空的 `BSU ESU` 单独发出、内容与移动都落在事务之外，于是 A/B/C/D 四组全部"正确"。
它**没有造出真实的失败条件**，因此绿色不能区分「无冲突」与「未触发」——正是
AGENTS.md 硬规则 2 点名的形状。

后续 `_probe_conpty_esu_cursor_drop.py` 加大了 payload，但那一版的期望值算错
（把 caret 行算成 24 而实际是 25），同样没有给出有效结论。

真正确认根因的是 `_probe_park_config_matrix.py` 的 A/B/C 配置对照：关掉同步输出后
光标回到 caret —— 但那不是「BSU/ESU 破坏光标语义」，而是**关掉同步输出使代码走
每帧 HIDE→SHOW 分支，于是 SHOW 被每帧发出**。根因是 SHOW 的发送频率，不是同步输出。

完整链路见 `docs/20261116-GA-中文输入法光标不跟输入框根因.md` §3。

以下是原始说明（保留）：

测什么
------
四个用例，唯一变量是「相对光标移动在 BSU/ESU 事务内还是外」：

  A 无 BSU/ESU        ：`AAA/BBB/CCC` + `ESC[2A ESC[3G` + SHOW
  B 移动在事务内      ：`BSU` + 同上内容与移动 + `ESU` + SHOW
  C 移动在事务外      ：`BSU` + 内容 + `ESU` + `ESC[2A ESC[3G` + SHOW
  D 移动在事务内+SHOW在内：GA 当前形态的等价物

判据（期望 col=2, row=1）
-------------------------
  B/D 偏离而 A/C 正确 → 同步输出与事务内光标移动不兼容，必须把 park 移出事务
  A/B/C/D 全对        → 不是同步输出的问题，另有原因

为什么必须做这个隔离
--------------------
`_probe_park_config_matrix.py` 已用真机对照证明：关掉 `GA_UI_SYNC_OUTPUT` 后原生光标
精确回到 caret（`(35,9)` 与反显块同列），打开时停在屏幕右下角 `(39,119)`。但那只说明
**相关性**。本探针把 App/ink 全部移除，只留序列，才能确认因果关系。

为什么必须保留
--------------
`docs/20261112-ga-ink-cursor-smoothness-fix-design.md` 引入同步输出时假定「BSU/ESU 只
影响可见性，不影响光标语义」。本探针是唯一能推翻（或确认）这个前提的机器判据；若将来
有人再把 park 移回事务内，它会立刻变红。

运行：python tests/probes/python/_probe_sync_output_cursor_conflict.py
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
COLS, ROWS = 80, 24

_guard = start(ROOT, quiet=True)

BSU = '\\u001b[?2026h'
ESU = '\\u001b[?2026l'
MOVE = '\\u001b[2A\\u001b[3G'   # 从帧底上移 2 行、列 3（0-based col=2）
SHOW = '\\u001b[?25h'
BODY = 'AAAA\\nBBBB\\nCCCC\\n'

CASES = {
    "A 无 BSU/ESU": f"{BODY}{MOVE}{SHOW}",
    "B 移动在事务内": f"{BSU}{BODY}{MOVE}{ESU}{SHOW}",
    "C 移动在事务外": f"{BSU}{BODY}{ESU}{MOVE}{SHOW}",
    "D 移动+SHOW 都在事务内": f"{BSU}{BODY}{MOVE}{SHOW}{ESU}",
}


def run(name, seq):
    env = dict(os.environ)
    for k in [k for k in env if k.upper().startswith(("CLAUDE", "ANTHROPIC"))]:
        env.pop(k, None)
    env["TERM"] = "xterm-256color"

    script = (
        "process.stdout.write('\\u001b[?1049h\\u001b[2J\\u001b[H');"
        f"process.stdout.write('{seq}');"
        "setTimeout(()=>{},400)"
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
    time.sleep(1.2)
    stop.set()
    try:
        p.terminate(force=True)
    except Exception:
        pass

    cur = (screen.cursor.y, screen.cursor.x)
    print(f"\n[{name}]")
    print(f"  cursor = {cur}   hidden={screen.cursor.hidden}")
    print("  屏幕:", [l.rstrip() for l in screen.display if l.strip()])
    print("  输出尾部:", repr("".join(raw_acc)[-160:]))
    return cur


try:
    res = {}
    for name, seq in CASES.items():
        res[name] = run(name, seq)
    print("\n=== 结论（期望 cursor=(1,2)，即 BBBB 行的第 3 列）===")
    for name, cur in res.items():
        ok = cur == (1, 2)
        print(f"  {'OK ' if ok else 'NG '} {name} → {cur}")
    b_ok = res["B 移动在事务内"] == (1, 2)
    c_ok = res["C 移动在事务外"] == (1, 2)
    d_ok = res["D 移动+SHOW 都在事务内"] == (1, 2)
    if c_ok and not b_ok:
        print("\n  → 判定：事务内的相对光标移动在 ConPTY 上被丢弃；park 必须移出 BSU/ESU")
    elif c_ok and b_ok and not d_ok:
        print("\n  → 判定：移动可以留在事务内，但 SHOW 必须在事务外")
    elif b_ok and d_ok:
        print("\n  → 判定：同步输出不冲突，真机偏移另有原因（与 matrix 结论矛盾，需复查）")
    else:
        print("\n  → 判定：结果不符合任一预期，需人工复查原始字节")
finally:
    finish(_guard, 0)
