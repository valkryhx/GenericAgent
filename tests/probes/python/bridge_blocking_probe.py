# -*- coding: utf-8 -*-
"""结构性证据：bridge 命令循环是单线程，任何同步里的长任务都会卡住它。

## 这个探针现在证明什么

它只证明**结构事实**：`run_jsonl_loop` 是 `for line in stdin`，
循环体里没有线程分派，所以「在循环同步路径上 sleep 多久，stop 就等多久」。
本探针用一个假 bridge 在 `submit` 里 sleep 来演示这一点 ——
它**不**测量真实压缩路径。

## 真实压缩路径的复验已换到另一个探针

压缩曾在这条同步路径上（`submit()` 内同步调 `_auto_compact_if_needed`），
已于 2026-11-14 移到后台线程。真实行为的复验见：

    python tests/probes/python/compact_off_loop_probe.py

那个用的是**真 bridge + 真 run_jsonl_loop**，能区分「修复前 / 修复后」；
本探针换成真 bridge 后会立刻变成 PASS 不了想要的判定，因为它测的是循环结构，
不是压缩实现。

## 做法（保持原样，作为结构回归）

伪造一个 `_auto_compact_if_needed` 让它 sleep N 秒，
然后从另一个线程往 stdin 写 `stop`，测量该命令被处理的延迟。

用法：python tests/probes/python/bridge_blocking_probe.py
"""
from __future__ import annotations

import io
import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(ROOT))


def main() -> int:
    out: list[str] = []
    say = lambda s: out.append(s)  # noqa: E731

    from frontends.ink import bridge as bridge_mod

    # 伪造压缩：让 _auto_compact_if_needed 睡 3 秒，模拟一次摘要请求
    SLEEP = 3.0
    processed: list[tuple[str, float]] = []
    t0 = time.monotonic()

    class FakeBridge:
        def __init__(self, emit):
            self.emit = emit
            self._stop_seen = False

        def emit_goal_snapshot(self): pass
        def emit_goal_nudge_if_any(self): pass

        def submit(self, text, **kw):
            # 关键：submit 里同步跑「压缩」
            self.emit({"type": "test", "where": "submit_enter", "t": time.monotonic() - t0})
            time.sleep(SLEEP)                     # ← 压缩占住循环
            self.emit({"type": "test", "where": "submit_exit", "t": time.monotonic() - t0})
            return 1

        def queue_prompt(self, text): pass
        def abort_current_turn(self):
            self._stop_seen = True
            processed.append(("stop", time.monotonic() - t0))
            self.emit({"type": "test", "where": "stop_handled", "t": time.monotonic() - t0})

    events: list[dict] = []

    def fake_emit(ev):
        events.append(ev)

    # 用一个可控 stdin：先给 submit，1 秒后再给 stop
    lines = iter([])          # 占位，实际用队列
    import queue as _q
    line_q: "_q.Queue[str]" = _q.Queue()

    class FakeStdin:
        def __iter__(self):
            while True:
                item = line_q.get()
                if item is None:
                    return
                yield item

    fake_bridge = FakeBridge(fake_emit)
    orig_cls = bridge_mod.GenericAgentBridge
    orig_ensure = getattr(bridge_mod.ga_paths, "ensure_runtime_dirs", lambda: None)

    say("=== 1. 前置：确认 run_jsonl_loop 是单线程 for line in stdin ===")
    src = Path(bridge_mod.__file__).read_text(encoding="utf-8")
    single_threaded = "for line in stdin" in src and "threading.Thread" not in src.split("for line in stdin")[1][:2000]
    say(f"  循环体里无线程分派 : {single_threaded}")

    say("")
    say("=== 2. 实验：压缩占住循环时，stop 的延迟 ===")
    say(f"  伪造压缩耗时 : {SLEEP}s")

    try:
        bridge_mod.GenericAgentBridge = lambda emit=None: fake_bridge  # type: ignore
        bridge_mod.ga_paths.ensure_runtime_dirs = lambda: None  # type: ignore

        def drive():
            # 0s: submit（会阻塞 3s）
            line_q.put(json.dumps({"type": "submit", "text": "hi"}) + "\n")
            # 1s: stop —— 此时循环应当还卡在 submit 里
            time.sleep(1.0)
            stop_sent_at = time.monotonic() - t0
            line_q.put(json.dumps({"type": "stop"}) + "\n")
            # 6s 后收工
            time.sleep(5.0)
            line_q.put(None)

        threading.Thread(target=drive, daemon=True).start()

        start = time.monotonic()
        bridge_mod.run_jsonl_loop(stdin=FakeStdin(), stdout=io.StringIO())
        elapsed = time.monotonic() - start
        say(f"  循环总耗时 : {elapsed:.1f}s")
    finally:
        bridge_mod.GenericAgentBridge = orig_cls  # type: ignore
        bridge_mod.ga_paths.ensure_runtime_dirs = orig_ensure  # type: ignore

    say("")
    say("=== 3. 事件时间线 ===")
    for ev in events:
        if ev.get("type") == "test":
            say(f"  t={ev['t']:5.2f}s  {ev['where']}")

    say("")
    say("=== 4. 判定 ===")
    stop_ev = next((e for e in events if e.get("where") == "stop_handled"), None)
    if stop_ev is None:
        say("  stop 未被处理 : FAIL（循环没读到它）")
        ok = False
    else:
        delay = stop_ev["t"] - 1.0     # stop 在 t=1.0s 发出
        say(f"  stop 发出时刻 : 1.00s")
        say(f"  stop 处理时刻 : {stop_ev['t']:.2f}s")
        say(f"  ⇒ 延迟        : {delay:.2f}s")
        # 若延迟 >= 压缩剩余时间（约 2s），说明确实被阻塞
        ok = delay >= 1.5
        say(f"  CHECK 被阻塞  : {'PASS' if ok else 'FAIL(没观察到阻塞)'}")

    say("")
    say("=== 结论 ===")
    if ok:
        say("压缩期间 bridge 命令循环被占，Esc（stop）要等压缩结束才被处理。")
        say("这解释了「界面看起来完全死了」——连中断都发不进去。")
    else:
        say("未观察到阻塞。可能循环已改为异步，或实验构造有误，需重新核对。")

    sys.stdout.buffer.write(("\n".join(out) + "\n").encode("utf-8"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
