# -*- coding: utf-8 -*-
"""【已知不可用 —— 保留作失败证据，别照这条路走】

PTY 路线验证「拖拽到视口外自动滚动」失败了。**能用的探针是同目录外的**
`frontends/ink/ui/probes/drag_scroll_real.ts`（真 App + 假流 + 屏幕仿真）。

## 为什么这条路走不通（实测抓包见 captures/drag_scroll.raw.txt）

1. **Ink 的粘贴层吃掉注入的字符**：注进去的文本被折叠成
   `[Copied text #1 +1 lines]`，于是提示词根本没提交成功；
2. **鼠标序列也没走到 GA 的处理函数**：SGR 报告进了 PTY，但拿不到任何
   滚动反应；
3. PTY 的读是阻塞的，`pump()` 里 `proc.read()` 会把整个探针挂死到超时
   （实测两次都是 143/124 退出码）。

## 保留它的理由

这条路上的每个失败点都容易重复踩：后面若有人想「用 PTY 验 TUI 交互」，
先看这里。可行路线请直接看 `drag_scroll_real.ts` 顶部的注释。

---
以下是原始设计（未走通）：

真机验证：拖拽到视口外会不会自动滚动，并扩展选区。

## 这个探针存在的理由

`selectionSpace` 的坐标换算有 26 条单测，但它们只证明「公式对」。
用户实测报的问题是**手感**：「无法自动随着滑动来上下扩展屏幕」。
手感只能在真终端里看：鼠标序列进不进得去、滚动跟不跟手、高亮贴不贴边。

## 做法

1. PTY 起真 `./ga`（不是 mock）；
2. 先要它输出足够多的行，造出可滚动内容；
3. 注入 SGR 鼠标序列：按下（视口内）→ 连续 drag（一路拖到视口上方）→ 释放；
4. 抓屏幕字节，比对「拖之前 / 拖之后」的可见文本首行是否变化
   —— 变了就说明真的滚了。

SGR 编码（见 src/mouseWheel.ts:130）：
  按下   ESC [ < 0 ; x ; y M      （button 0 = 左键，1-based 坐标）
  拖动   ESC [ < 32 ; x ; y M     （button | 0x20 = 左键按住移动）
  释放   ESC [ < 0 ; x ; y m      （小写 m）

用法：python tests/probes/python/drag_scroll_probe.py
产出：tests/probes/captures/drag_scroll.txt
"""
from __future__ import annotations

import os
import re
import sys
import time
from pathlib import Path

from winpty import PtyProcess

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _workspace_guard import finish, start  # noqa: E402

ROOT = r"D:\git_repos\GenericAgent"
OUT_DIR = os.path.join(ROOT, "tests", "probes", "captures")
ROWS, COLS = 30, 100

# 造内容的提示词：要求输出很多行，方便滚
PROMPT = "请从 1 数到 60，每行一个数字，只输出数字本身，不要任何解释。"


def sgr(button: int, x: int, y: int, release: bool = False) -> str:
    """SGR 鼠标报告（1-based 坐标）。"""
    return f"\x1b[<{button};{x};{y}{'m' if release else 'M'}"


def plain(text: str) -> str:
    """去 ANSI，只要可见字符。"""
    text = re.sub(r"\x1b\[[0-9;?]*[A-Za-z]", "", text)
    text = re.sub(r"\x1b\][^\x07]*\x07", "", text)
    text = re.sub(r"\x1b[()][A-Z0-9]", "", text)
    return text


def visible_numbers(text: str) -> list[int]:
    """抽出屏幕上可见的纯数字行 —— 用来判断「滚到哪了」。"""
    nums = []
    for line in plain(text).splitlines():
        s = line.strip()
        if re.fullmatch(r"\d{1,3}", s):
            nums.append(int(s))
    return nums


def main() -> int:
    os.makedirs(OUT_DIR, exist_ok=True)
    guard = start(ROOT)
    out: list[str] = []
    say = lambda s: out.append(s)  # noqa: E731

    env = dict(os.environ)
    for k in [k for k in env if k.upper().startswith(("CLAUDE", "ANTHROPIC"))]:
        env.pop(k, None)
    env["PYTHONPATH"] = ROOT
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    env["GA_INK_MOUSE"] = "full"          # 必须 full：拖动才被 GA 接管
    env["GA_TUI_FULLSCREEN"] = "1"        # 全屏才有滚动视口

    proc = PtyProcess.spawn(
        ["bash", "-lc", "cd /d/git_repos/GenericAgent && ./ga"],
        dimensions=(ROWS, COLS),
        env=env,
        cwd=ROOT,
    )

    buf: list[str] = []

    def pump(seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            try:
                raw = proc.read(65536)
            except EOFError:
                return
            if raw:
                buf.append(raw)
            else:
                time.sleep(0.03)

    def screen() -> str:
        return "".join(buf)

    try:
        say("=== 1. 启动 ===")
        pump(12)
        say("启动后可见数字: %r" % (visible_numbers(screen())[:10],))

        say("")
        say("=== 2. 提交长输出 prompt ===")
        proc.write(PROMPT + "\r")
        pump(45)

        before = screen()
        nums_before = visible_numbers(before)
        say("输出后可见数字（去重排序后首 8 个）: %r" % (sorted(set(nums_before))[:8],))
        say("输出后可见数字（末 8 个）: %r" % (sorted(set(nums_before))[-8:]))

        if len(set(nums_before)) < 5:
            say("")
            say("!! 内容太少，无法验证滚动（需要 >=5 个数字行）")
            return report(out, guard, screen())

        # 记下拖动前屏幕上的数字快照
        snapshot_before = sorted(set(nums_before))

        say("")
        say("=== 3. 拖拽：视口内按下 → 一路拖到顶部 → 释放 ===")
        # 按下：第 10 行、第 5 列（视口内部）
        proc.write(sgr(0, 5, 10))
        pump(0.6)
        # 连续 drag，一路往左上拖出视口顶部
        for step, y in enumerate((8, 6, 4, 2, 1)):
            proc.write(sgr(32, 5, y))
            pump(0.5)
            say("  drag -> y=%d" % y)
        # 再多拖几次，让自动滚动累积
        for extra in range(6):
            proc.write(sgr(32, 5, 1))
            pump(0.4)
        say("  在 y=1 上额外拖了 6 次")
        proc.write(sgr(0, 5, 1, release=True))
        pump(1.5)

        after = screen()
        nums_after = visible_numbers(after)
        snapshot_after = sorted(set(nums_after))

        say("")
        say("=== 4. 判定 ===")
        say("拖动前可见数字: %r" % (snapshot_before,))
        say("拖动后可见数字: %r" % (snapshot_after,))

        scrolled = snapshot_before != snapshot_after
        say("可见内容发生变化: %s" % ("是" if scrolled else "否"))

        # 滚动的方向：应能看到更早（更小）的数字
        if scrolled:
            earlier = set(snapshot_after) - set(snapshot_before)
            smaller = [n for n in earlier if snapshot_before and n < min(snapshot_before)]
            say("新出现的数字: %r" % (sorted(earlier)[:10],))
            say("其中比原首行更早的: %r" % (sorted(smaller)[:10],))
            say("CHECK 滚动发生 : PASS")
            say("CHECK 方向朝历史: %s" % ("PASS" if smaller else "FAIL(方向可能反了)"))
        else:
            say("CHECK 滚动发生 : FAIL")

        return report(out, guard, screen())
    finally:
        try:
            proc.terminate(force=True)
        except Exception:
            pass


def report(out: list[str], guard, raw: str = "") -> int:
    text = "\n".join(out)
    sys.stdout.buffer.write((text + "\n").encode("utf-8"))
    path = os.path.join(OUT_DIR, "drag_scroll.txt")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text + "\n")
    # 原始抓包单独存，诊断「为什么没抽到数字」时唯一有用的东西
    if raw:
        rawpath = os.path.join(OUT_DIR, "drag_scroll.raw.txt")
        with open(rawpath, "w", encoding="utf-8", newline="\n") as f:
            f.write(raw)
        sys.stdout.buffer.write(("raw -> %s (%d chars)\n" % (rawpath, len(raw))).encode("utf-8"))
    sys.stdout.buffer.write(("\n-> %s\n" % path).encode("utf-8"))
    finish(guard)
    return 0


if __name__ == "__main__":
    sys.exit(main())
