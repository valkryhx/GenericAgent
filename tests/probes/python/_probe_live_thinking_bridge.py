# -*- coding: utf-8 -*-
"""探针：`assistant_thinking` 事件是否真的从 bridge 出去了。

## 测什么

直接调 `GenericAgentBridge._bind_thinking()` + `_on_llm_thinking()`，截获它
emit 的事件，断言：

1. `on_thinking` 被挂到 backend 上（接线这一环最容易漏）。
2. 文本被转成 `assistant_thinking` 事件，`taskId` 对应当前回合。
3. 纯空白 payload 不产生事件。
4. 对照组：没接线的 backend（`on_thinking is None`）不炸，行为与以前一致。

## 为什么不走真 LLM

这个探针只管 bridge 的投影逻辑，变量应该只有一个。真 LLM 那一侧已由
`tests/test_llm_thinking.py`（解析层）+ 一次真实 `raw_ask` 冒烟分开验证。
把两者混在一起，失败了分不清是网络、provider 还是 bridge。

## 为什么必须留着

「on_thinking 被挂上」这件事没有任何静态检查能保证：`_bind_thinking` 里
`getattr` 链条一旦写错（比如取 `agent.llmclient` 而不是 `.backend`），
症状是**静默无 thinking**，而不是报错。这条断言是唯一能提前抓住它的东西。

运行：python tests/probes/python/_probe_live_thinking_bridge.py
"""
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "frontends"))

from frontends.ink import bridge as B  # noqa: E402


class Backend:
    def __init__(self):
        self.on_thinking = None
        self.on_retry_status = None


class Client:
    def __init__(self, backend):
        self.backend = backend


class Agent:
    def __init__(self, backend):
        self.llmclient = Client(backend)


class BridgeStub:
    """Just enough of GenericAgentBridge to exercise the thinking path."""

    _task_seq = 7
    _bind_thinking = B.GenericAgentBridge._bind_thinking
    _on_llm_thinking = B.GenericAgentBridge._on_llm_thinking

    def __init__(self):
        self.emitted = []

    def emit(self, event):
        self.emitted.append(event)


def main() -> int:
    failures = 0

    # ── 1. 接线 ────────────────────────────────────────────────────────
    backend = Backend()
    stub = BridgeStub()
    stub._bind_thinking(Agent(backend))
    if callable(backend.on_thinking):
        print("✔ _bind_thinking 把 on_thinking 挂到了 backend 上")
    else:
        failures += 1
        print("✖ _bind_thinking 没有挂上 on_thinking —— 实时思考会静默消失")

    # ── 2. 转发 ────────────────────────────────────────────────────────
    backend.on_thinking({"text": "REASON"})
    if stub.emitted == [{"type": "assistant_thinking", "taskId": 7, "text": "REASON"}]:
        print("✔ payload → assistant_thinking 事件，taskId 取自 _task_seq")
    else:
        failures += 1
        print(f"✖ 事件不对: {stub.emitted}")

    # ── 3. 空白过滤 ────────────────────────────────────────────────────
    stub.emitted.clear()
    backend.on_thinking({"text": "   \n "})
    if not stub.emitted:
        print("✔ 纯空白推理不产生事件")
    else:
        failures += 1
        print(f"✖ 空白推理漏出去了: {stub.emitted}")

    # ── 4. 对照组：没有 backend 时不炸 ─────────────────────────────────
    stub2 = BridgeStub()
    try:
        stub2._bind_thinking(Agent(None))
        stub2._on_llm_thinking({"text": "X"})
        print(f"✔ 无 backend 时不抛异常（仍发了 {len(stub2.emitted)} 条事件）")
    except Exception as exc:
        failures += 1
        print(f"✖ 无 backend 时崩溃: {type(exc).__name__}: {exc}")

    print()
    print("✔ 全部通过" if failures == 0 else f"✖ {failures} 项失败")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
