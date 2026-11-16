# -*- coding: utf-8 -*-
"""探针：GA 三种配置下，ConPTY 出口处「原生光标最终落在哪」（配置 A/B/C 对照）。

测什么
------
同一台机器、同一终端尺寸、同一输入序列，只改一个环境变量，各跑一次，读回
pyte 看到的原生光标绝对位置与 composer caret cell：

  A `GA_UI_PARK=on    GA_UI_SYNC_OUTPUT` 不设   （产品默认）
  B `GA_UI_SYNC_OUTPUT=0`                       （关掉 DEC 2026）
  C `GA_UI_CURSOR_PARK=off`                     （完全不 park，旧行为）

判据
----
  光标 == caret cell  → 该配置在 ConPTY 上正确
  否则                → 该配置下终端认为光标在别处，输入法 preedit 就会画在那里

为什么必须这样做
----------------
`_probe_replay_real_frame.py` 把 GA 自己写出的整帧重放，光标精确落在 caret；
`_probe_park_spec_real.py` 也证明 SINK 末尾确实带 `ESC[4A ESC[9G`。真人会话里却读到
(39,119)。说明差别不在单帧字节，而在「多帧 + BSU/ESU + ConPTY 差异重绘」的交互，
只有按配置对照才能定位到具体开关。

为什么必须保留
--------------
这是唯一把「park 算术」「同步输出」「ConPTY 重绘」三者分开的对照实验。若 B 正确而 A
错误，那么结论是 DEC 2026 在 ConPTY 下与相对光标移动不兼容 —— 这与
`docs/20261112-ga-ink-cursor-smoothness-fix-design.md` 引入同步输出时的假设相反，
必须留档。

运行：python tests/probes/python/_probe_park_config_matrix.py
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

_guard = start(ROOT, quiet=True)

CONFIGS = {
    "A 默认（park + 同步输出）": {"GA_UI_PARK_LOG": os.path.join(ROOT, "temp", "matrix_A.log")},
    "B 关同步输出（GA_UI_SYNC_OUTPUT=0）": {"GA_UI_SYNC_OUTPUT": "0", "GA_UI_PARK_LOG": os.path.join(ROOT, "temp", "matrix_B.log")},
    "C 关 park（GA_UI_CURSOR_PARK=off）": {"GA_UI_CURSOR_PARK": "off"},
}


def run(name, extra_env):
    env = dict(os.environ)
    for k in [k for k in env if k.upper().startswith(("CLAUDE", "ANTHROPIC"))]:
        env.pop(k, None)
    env.pop("GA_UI_SYNC_OUTPUT", None)
    env.pop("GA_UI_CURSOR_PARK", None)
    env["PYTHONPATH"] = ROOT
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["TERM"] = "xterm-256color"
    env.update(extra_env)

    screen = pyte.Screen(COLS, ROWS)
    stream = pyte.Stream(screen)
    lock = threading.Lock()
    stop = threading.Event()
    meter = {"show": 0, "hide": 0}

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
                time.sleep(0.01)
                continue
            with lock:
                text = raw if isinstance(raw, str) else raw.decode("utf-8", "replace")
                meter["show"] += len(re.findall(r"\x1b\[\?25h", text))
                meter["hide"] += len(re.findall(r"\x1b\[\?25l", text))
                try:
                    stream.feed(raw)
                except Exception:
                    pass

    threading.Thread(target=reader, daemon=True).start()

    def locate():
        with lock:
            disp = list(screen.display)
            cur = (screen.cursor.y, screen.cursor.x)
            buf = {y: {x: screen.buffer[y][x] for x in range(COLS)} for y in range(ROWS)}
        for y in range(len(disp) - 2, -1, -1):
            if re.fullmatch(r"─{8,}", disp[y].strip()) and disp[y + 1].lstrip().startswith(">"):
                row = y + 1
                rev = [x for x in range(COLS) if buf[row][x].reverse]
                return row, (rev[0] if rev else None), cur
        return None, None, cur

    try:
        for _ in range(70):
            time.sleep(0.4)
            if locate()[0] is not None:
                break
        time.sleep(1.2)
        for ch in "abcdef":
            p.write(ch)
            time.sleep(0.15)
        time.sleep(2.0)
        row, block, cur = locate()
        disp = list(screen.display)
        line = disp[row] if row is not None else ""
        print(f"\n[{name}]")
        print(f"  composer 输入行 = {row}  反显块列 = {block}")
        print(f"  该行内容 = {line.rstrip()[:60]!r}")
        print(f"  原生光标 = {cur}   DECTCEM show={meter['show']} hide={meter['hide']}")
        if row is not None and block is not None:
            aligned = (cur[0] == row and cur[1] == block)
            print(f"  → {'OK 光标在 caret' if aligned else '偏离 row=%+d col=%+d' % (cur[0]-row, cur[1]-block)}")
            return aligned
        print("  → 未定位到 composer")
        return None
    finally:
        stop.set()
        try:
            p.terminate(force=True)
        except Exception:
            pass
        time.sleep(0.3)


try:
    results = {}
    for name, extra in CONFIGS.items():
        results[name] = run(name, extra)
    print("\n=== 汇总 ===")
    for name, ok in results.items():
        print(f"  {name}: {'光标在 caret' if ok else ('偏离' if ok is False else '未测到')}")
finally:
    finish(_guard, 0)
