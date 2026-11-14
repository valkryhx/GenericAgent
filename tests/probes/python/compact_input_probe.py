# -*- coding: utf-8 -*-
"""核实：GA 在自动压缩期间，用户的输入到底会怎样。

## 为什么必须单独核实

对 CC 的调研结论是「压缩期输入不被禁用、消息排队等压缩完」
（`canDeliverOverTurn` 显式放行 `isCompacting`）。
但那是 CC 的行为。GA 是不是也一样，**不能按 CC 假设** —— 用户报告
「用户也无法此刻输入」，需要拿 GA 的源码事实来判断。

## 结论（先写在这里，下面的探针负责复现）

GA 的行为是：**拒绝，而不是排队**。

`frontends/ink/bridge.py:576`：

```python
def submit(self, text, display_text=None, images=None) -> int:
    ...
    if getattr(self.agent, "is_running", False) or self._is_consuming():
        self.emit({"type": "error", "code": "busy", "message": "agent is running"})
        return -1
```

而自动压缩发生在 `GenericAgent.run()` 里、`self.is_running = True` **之前**
（`app.py:1955-1956` 调 `_auto_compact_before_task`，`app.py:1990` 才置
`is_running = True`）。但 `submit()` 早在那之前就已经启动了
`_consume_thread`（`bridge.py:652`）并一直活着到回合结束 ——
所以压缩期间 `self._is_consuming()` 为真。

⇒ **压缩期间发消息会收到 `{"type":"error","code":"busy"}`，输入被丢弃，不排队。**

对比 CC：CC 是 `wait`（保留排队），GA 是 reject（丢弃）。

## 附带事实：压缩期间前端收不到任何事件

`compact_agent_context` / `maybe_auto_compact` 全程没有向 `display_queue`
或 emit 通道写任何东西（`grep compact | grep emit` 零命中）。
所以 UI 侧既没有「正在压缩」的文案，也没有心跳 —— 这就是「界面静止」的根因。

用法：python tests/probes/python/compact_input_probe.py
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))


def main() -> int:
    out: list[str] = []
    say = lambda s: out.append(s)  # noqa: E731
    ok = True

    say("=== 1. 源码事实：submit() 的忙检查 ===")
    src = Path(__file__).resolve().parents[3] / "frontends" / "ink" / "bridge.py"
    text = src.read_text(encoding="utf-8")
    has_busy = 'code": "busy"' in text or "'code': 'busy'" in text
    has_consuming_guard = "self._is_consuming()" in text
    say(f"  submit() 里有 busy 拒绝 : {has_busy}")
    say(f"  条件含 _is_consuming()   : {has_consuming_guard}")
    ok = ok and has_busy and has_consuming_guard

    say("")
    say("=== 2. 源码事实：压缩发生在 is_running 置真之前 ===")
    app = (Path(__file__).resolve().parents[3] / "genericagent" / "core" / "app.py").read_text(encoding="utf-8")
    lines = app.split("\n")
    compact_line = next(
        (i for i, ln in enumerate(lines, 1) if "_auto_compact_before_task(raw_query, display_queue)" in ln),
        None,
    )
    running_line = next(
        (i for i, ln in enumerate(lines, 1) if i > (compact_line or 0) and "self.is_running = True" in ln),
        None,
    )
    say(f"  _auto_compact_before_task 调用行 : {compact_line}")
    say(f"  之后第一个 is_running = True 行 : {running_line}")
    before = compact_line is not None and running_line is not None and compact_line < running_line
    say(f"  压缩在前、置真在后               : {before}")
    ok = ok and before

    say("")
    say("=== 3. 源码事实：压缩期间零事件 ===")
    ctx = (Path(__file__).resolve().parents[3] / "genericagent" / "core" / "context.py").read_text(encoding="utf-8")
    emit_hits = [
        ln for ln in ctx.split("\n")
        if "compact" in ln.lower() and ("emit" in ln or "display_queue" in ln or "put(" in ln)
    ]
    say(f"  压缩代码里发事件的语句数 : {len(emit_hits)}")
    no_events = len(emit_hits) == 0
    say(f"  ⇒ 前端收不到压缩状态     : {no_events}")
    ok = ok and no_events

    say("")
    say("=== 4. 运行时复现：压缩期间 submit 会被拒 ===")
    say("  注意：压缩阶段 is_running 仍是 False，真正的拦截来自 _is_consuming()。")
    say("  这两者是不同的守卫 —— 修的时候要改对地方。")
    # 不依赖真 LLM：直接把 bridge 对象造出来，模拟「consume 线程活着」
    from frontends.ink.bridge import GenericAgentBridge  # noqa: E402
    from unittest import mock

    agent = mock.MagicMock()
    agent.is_running = False              # 压缩阶段 is_running 还是假的
    bridge = GenericAgentBridge.__new__(GenericAgentBridge)
    bridge.agent = agent
    emitted: list[dict] = []
    bridge.emit = lambda ev: emitted.append(ev)

    # 模拟 consume 线程存活（压缩期间它本来就是活的）。
    # 注意顺序：event 必须**先 clear 再 start**，否则线程看到已 set 立刻退出，
    # `is_alive()` 变假 → 忙检查不命中 → 探针会误报「能提交」。
    alive = threading.Event()
    thread = threading.Thread(target=alive.wait, daemon=True)
    thread.start()
    time.sleep(0.1)
    if not thread.is_alive():
        say("  !! 模拟线程没活着，检查无效")
        return 1
    bridge._consume_thread = thread

    rc = bridge.submit("这条消息在压缩期间发出")
    say(f"  submit 返回 : {rc}")
    codes = [e.get("code") for e in emitted if e.get("type") == "error"]
    say(f"  发出的 error code : {codes}")
    rejected = rc == -1 and "busy" in codes
    say(f"  ⇒ 被拒绝（不是排队）: {rejected}")
    ok = ok and rejected

    alive.clear()
    thread.join(timeout=2)

    say("")
    say("=== 结论 ===")
    if ok:
        say("GA 压缩期间：输入被**拒绝**（error/busy），不是排队。")
        say("与 CC 不同 —— CC 是 wait（排队），GA 是 reject（丢弃）。")
        say("且压缩期间前端收不到任何事件，所以界面完全静止。")
    else:
        say("!! 有检查项不成立，结论需要重新核对")

    sys.stdout.buffer.write(("\n".join(out) + "\n").encode("utf-8"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
