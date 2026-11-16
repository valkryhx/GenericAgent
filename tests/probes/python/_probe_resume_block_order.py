# -*- coding: utf-8 -*-
"""探针：resume 后同一回合内的块顺序是否与真实发生顺序一致。

## 测什么

喂一个已知块序列的 v2 会话（`thinking → text → tool_use`，再跟一条
`tool_result` 的 user 消息，再一个 `thinking → text → tool_use`），然后看
`load_session()` 产出的 `TranscriptTurn` 和 bridge 发出的 `history_replace`
消息，能不能还原原始顺序。

## 用户实测症状

「所有 thinking 都在最前，所有 toolcall 都在最后」。根因是 `load_v2` 按**类型**
把同一回合的块压平成三个数组（`thinking[]`、`tool_uses[]`、`tool_results[]`），
块之间的相对顺序在这一步就丢了；前端只能按固定顺序画，于是必然错。

## 为什么是确定性的

自己写一个临时 v2 会话文件，喂固定块序列，读纯函数产出。没有网络、没有计时。

## 为什么必须留着

「顺序」这件事没有任何类型或静态检查能保证：三个数组各自都完整、条数也对，
只有把它们**放在一起看相对位置**才能发现错了。单看每个数组的条数断言（现有的
`ResumeToolUseTest` / `ResumeThinkingTest` 就是这种）对这个 bug 全绿。

运行：python tests/probes/python/_probe_resume_block_order.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))
sys.path.insert(0, str(REPO / "frontends"))

from genericagent.core import transcripts as T  # noqa: E402


def write_session(path: Path) -> None:
    """一个两轮的 v2 会话，块顺序刻意做成「交错」的，压平后必然错乱。"""
    events = [
        {"version": 2, "type": "session_start", "session_id": "probe-order", "seq": 1},
        # turn 1: user 提问
        {"version": 2, "type": "message", "uuid": "u1", "turn_id": 1, "role": "user",
         "origin": {"kind": "human"}, "blocks": [{"type": "text", "text": "查一下"}]},
        # turn 1: assistant 先思考 → 再说话 → 再调工具
        {"version": 2, "type": "message", "uuid": "a1", "turn_id": 1, "role": "assistant",
         "origin": {"kind": "assistant"}, "blocks": [
             {"type": "thinking", "thinking": "THINK-1"},
             {"type": "text", "text": "先搜一下。"},
             {"type": "tool_use", "id": "c1", "name": "web_search", "input": {"q": "x"}},
         ]},
        # 工具结果
        {"version": 2, "type": "message", "uuid": "r1", "turn_id": 1, "role": "user",
         "origin": {"kind": "tool_result"}, "blocks": [
             {"type": "tool_result", "tool_use_id": "c1", "content": "RESULT-1"},
         ]},
        # assistant 再思考 → 再说话 → 再调工具
        {"version": 2, "type": "message", "uuid": "a2", "turn_id": 1, "role": "assistant",
         "origin": {"kind": "assistant"}, "blocks": [
             {"type": "thinking", "thinking": "THINK-2"},
             {"type": "text", "text": "再查一次。"},
             {"type": "tool_use", "id": "c2", "name": "web_fetch", "input": {"url": "y"}},
         ]},
        {"version": 2, "type": "message", "uuid": "r2", "turn_id": 1, "role": "user",
         "origin": {"kind": "tool_result"}, "blocks": [
             {"type": "tool_result", "tool_use_id": "c2", "content": "RESULT-2"},
         ]},
        {"version": 2, "type": "turn_end", "turn_id": 1, "first_uuid": "u1", "last_uuid": "r2"},
    ]
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for event in events:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "probe_order.jsonl"
        write_session(path)
        loaded = T.load_session(path)

    turn = loaded.turns[0]
    print(f"turn 1: user={turn.user_text!r}")
    print(f"        assistant_text={turn.assistant_text!r}")
    print(f"        thinking={turn.thinking}")
    print(f"        tool_uses={[u['tool'] for u in turn.tool_uses]}")
    print(f"        tool_results={[r['toolUseId'] for r in turn.tool_results]}")
    print()

    failures = 0

    # 1. 数据完整性（这些现有的测试已经覆盖，列出来是为了把「对的」和「顺序」分开）
    if turn.thinking == ["THINK-1", "THINK-2"]:
        print("✔ 两个 thinking 块都在")
    else:
        failures += 1
        print(f"✖ thinking 内容不对: {turn.thinking}")

    if [u["tool"] for u in turn.tool_uses] == ["web_search", "web_fetch"]:
        print("✔ 两个 tool_use 都在")
    else:
        failures += 1
        print(f"✖ tool_uses 不对: {turn.tool_uses}")

    # 2. 顺序：必须有一个能表达「THINK-1 → 先搜一下 → web_search → THINK-2 → 再查一次 → web_fetch」
    #    的单一序列。三个独立数组做不到，所以要有 blocks 字段。
    blocks = getattr(turn, "blocks", None)
    if blocks is None:
        failures += 1
        print("✖ TranscriptTurn 没有 blocks 字段 —— 顺序信息在加载层就丢了")
        print("   （这正是「所有 thinking 在最前、所有 tool 在最后」的根因）")
    else:
        kinds = [(b.get("kind") or b.get("type")) for b in blocks]
        print(f"blocks 种类序列: {kinds}")
        expected = ["thinking", "text", "tool_use", "tool_result",
                    "thinking", "text", "tool_use", "tool_result"]
        if kinds == expected:
            print("✔ blocks 保留了真实交错顺序")
        else:
            failures += 1
            print(f"✖ blocks 顺序不对，期望 {expected}")

    print()
    print("✔ 全部通过" if failures == 0 else f"✖ {failures} 项失败（顺序信息未保留）")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
