# -*- coding: utf-8 -*-
"""核实：压缩不再占住 bridge 命令循环（Esc / queue_prompt 可读）。

## 背景

`20261114-GA压缩期输入六路径核实.md` §2 实证过旧行为：
`submit()` 内部同步调 `_auto_compact_if_needed()`，而 `run_jsonl_loop` 是
单线程 `for line in stdin`，所以压缩期间 `stop`（Esc）要等压缩结束才被处理。

`frontends/ink/bridge.py` 已把压缩移到后台线程（`_begin_compaction_gate` /
`_compact_then_start_turn`）。本探针用**真 bridge + 真 run_jsonl_loop**
复验：压缩期间发 `stop`，它必须立刻被处理，而不是等压缩跑完。

## 做法

- 真 `GenericAgentBridge`，只把 `compact_agent_context` 换成一个可阻塞的假实现；
- 真 `run_jsonl_loop`，stdin 用可控队列喂；
- 记录 `submit` 返回时刻、压缩开始时刻、`stop` 被处理时刻。

用法：python tests/probes/python/compact_off_loop_probe.py
"""
from __future__ import annotations

import io
import json
import queue as _q
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "frontends"))
sys.path.insert(0, str(ROOT / "tests"))

COMPACT_SECONDS = 3.0
STOP_SENT_AT = 1.0


def main() -> int:
    out: list[str] = []
    say = lambda s: out.append(s)  # noqa: E731

    import frontends.ink.bridge as bridge_mod
    from tests.test_ink_bridge import FakeAgent

    timeline: list[tuple[str, float]] = []
    t0 = time.monotonic()

    def mark(where: str) -> None:
        timeline.append((where, time.monotonic() - t0))

    agent = FakeAgent()
    real_bridge = bridge_mod.GenericAgentBridge(
        agent_factory=lambda: agent,
        emit=lambda ev: None,
    )

    # 记录 Esc 路径被调用的时刻：包一层 abort_current_turn。
    real_abort = real_bridge.abort_current_turn

    def timed_abort() -> None:
        mark("stop_handled")
        real_abort()

    real_bridge.abort_current_turn = timed_abort  # type: ignore

    real_submit = real_bridge.submit

    def timed_submit(text: str, **kw) -> int:
        mark("submit_enter")
        rc = real_submit(text, **kw)
        mark("submit_exit")
        return rc

    real_bridge.submit = timed_submit  # type: ignore

    def slow_compact(*args, **kwargs):
        mark("compact_enter")
        time.sleep(COMPACT_SECONDS)
        mark("compact_exit")
        result = type("R", (), {})()
        result.ok = True
        result.message = "Compacted (probe)."
        return result

    line_q: "_q.Queue[str | None]" = _q.Queue()

    class FakeStdin:
        def __iter__(self):
            while True:
                item = line_q.get()
                if item is None:
                    return
                yield item

    def drive() -> None:
        line_q.put(json.dumps({"type": "submit", "text": "large prompt"}) + "\n")
        time.sleep(STOP_SENT_AT)
        mark("stop_sent")
        line_q.put(json.dumps({"type": "stop"}) + "\n")
        time.sleep(COMPACT_SECONDS + 2.0)
        line_q.put(None)

    say("=== 1. 前置：压缩已不在命令循环上 ===")
    src = Path(bridge_mod.__file__).read_text(encoding="utf-8")
    submit_src = src.split("def submit(", 1)[1].split("    def _start_turn", 1)[0]
    inline_compact = "compact_agent_context(" in submit_src
    say(f"  submit() 内直接调用 compress : {inline_compact}   (期望 False)")
    threaded = "_compaction_thread = threading.Thread" in src
    say(f"  压缩在独立线程启动          : {threaded}   (期望 True)")

    say("")
    say(f"=== 2. 实验：压缩 {COMPACT_SECONDS:.0f}s，t={STOP_SENT_AT:.0f}s 发 stop ===")
    orig_cls = bridge_mod.GenericAgentBridge
    orig_ensure = bridge_mod.ga_paths.ensure_runtime_dirs
    try:
        bridge_mod.GenericAgentBridge = lambda **kw: real_bridge  # type: ignore
        bridge_mod.ga_paths.ensure_runtime_dirs = lambda: None  # type: ignore
        threading.Thread(target=drive, daemon=True).start()
        with (
            patch("frontends.ink.bridge.should_auto_compact_agent", return_value=True),
            patch("frontends.ink.bridge.compact_agent_context", side_effect=slow_compact),
        ):
            bridge_mod.run_jsonl_loop(stdin=FakeStdin(), stdout=io.StringIO())
    finally:
        bridge_mod.GenericAgentBridge = orig_cls  # type: ignore
        bridge_mod.ga_paths.ensure_runtime_dirs = orig_ensure  # type: ignore
        # 清掉可能残留的压缩线程，避免拖住进程退出
        real_bridge._compaction_cancel.set()

    say("")
    say("=== 3. 事件时间线 ===")
    for where, at in timeline:
        say(f"  t={at:5.2f}s  {where}")

    say("")
    say("=== 4. 判定 ===")
    by_name = {where: at for where, at in timeline}
    ok = True

    submit_exit = by_name.get("submit_exit")
    if submit_exit is None:
        say("  submit 未返回           : FAIL")
        ok = False
    else:
        say(f"  submit 返回耗时         : {submit_exit - by_name['submit_enter']:.2f}s"
            f"   (期望 < 0.5s)")
        if submit_exit - by_name["submit_enter"] >= 0.5:
            say("  CHECK submit 不被阻塞    : FAIL")
            ok = False
        else:
            say("  CHECK submit 不被阻塞    : PASS")

    stop_at = by_name.get("stop_handled")
    if stop_at is None:
        say("  stop 未被处理           : FAIL（循环没读到它）")
        ok = False
    else:
        delay = stop_at - by_name["stop_sent"]
        say(f"  stop 处理延迟           : {delay:.2f}s   (期望 < 0.5s)")
        if delay >= 0.5:
            say("  CHECK Esc 立即可用       : FAIL（仍被阻塞）")
            ok = False
        else:
            say("  CHECK Esc 立即可用       : PASS")

    compact_exit = by_name.get("compact_exit")
    if compact_exit is None:
        say("  压缩实际执行            : FAIL（没跑到）")
        ok = False
    else:
        say(f"  压缩仍在后台跑完         : PASS（t={compact_exit:.2f}s）")

    say("")
    say("=== 结论 ===")
    if ok:
        say("压缩已移到后台线程：submit 立即返回，Esc(stop) 在压缩期间即可处理。")
    else:
        say("修复未生效，压缩仍占住命令循环 —— 重新核对 _begin_compaction_gate。")

    sys.stdout.buffer.write(("\n".join(out) + "\n").encode("utf-8"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
