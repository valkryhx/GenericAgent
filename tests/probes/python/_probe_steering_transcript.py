# -*- coding: utf-8 -*-
"""探针：steering（回合中插话）的问题有没有进 transcript，resume 后能不能看到。

## 测什么

直接构造 transcript 的两个场景，然后 `load_session()` 回来，看 `user_text` 是什么：

A. 一个回合里先有一条人类提问，后面又来一条人类消息（steering 的真实落盘形态）。
B. 对照：单条提问的普通回合。

## 用户实测症状

「steering 时插入的问题 agent 明明回答了，但是 resume 时看不到我的提问」。

## 根因坐标

`genericagent/core/transcripts.py:load_v2` 的 user 分支用的是**赋值**：

    if role == "user" and not (origin.kind == "tool_result"):
        if value:
            turn_text.setdefault(int(turn_id), {})["user_text"] = value

一个回合内多条人类提问时，后一条**覆盖**前一条 —— 和 `assistant_text` 之前那个
「只留最后一步正文」的 bug 完全同型（那个已经修成拼接，user 侧没跟着修）。

`bridge._resume_ui_messages_with_checkpoints` 每个 turn 只发一条 user 消息，所以
被覆盖掉的那条在 resume 界面上就整个消失了。

## 为什么是确定性的

自己写临时 v2 会话文件，喂固定事件序列，读纯函数产出。没有网络、没有计时。

## 为什么必须留着

「消息丢了」和「消息顺序不对」都不会报错：transcript 里两个 message 事件都在、
`rounds` 也是对的，只有把 `user_text` 读出来跟原始事件逐个比对才能发现少了一条。
现有测试没有任何一条覆盖「一个回合多条人类提问」。

运行：python tests/probes/python/_probe_steering_transcript.py
"""
import json
import sys
import tempfile
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))

from genericagent.core import transcripts as T  # noqa: E402
from genericagent.core.pending_input import WRAP_PREFIX, WRAP_SUFFIX  # noqa: E402


def write_session(path: Path, events) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as fh:
        for event in events:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def base_events(turn_id=1):
    return [{"version": 2, "type": "session_start", "session_id": "probe-steer", "seq": 1}]


def main() -> int:
    failures = 0

    # ── A: 一个回合内有两条人类消息（steering） ──────────────────────
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "steer.jsonl"
        events = base_events() + [
            {"version": 2, "type": "message", "uuid": "u1", "turn_id": 1, "role": "user",
             "origin": {"kind": "human"}, "blocks": [{"type": "text", "text": "第一个问题"}]},
            {"version": 2, "type": "message", "uuid": "a1", "turn_id": 1, "role": "assistant",
             "origin": {"kind": "assistant"},
             "blocks": [{"type": "tool_use", "id": "c1", "name": "code_run", "input": {}}]},
            {"version": 2, "type": "message", "uuid": "r1", "turn_id": 1, "role": "user",
             "origin": {"kind": "tool_result"},
             "blocks": [{"type": "tool_result", "tool_use_id": "c1", "content": "OUT"}]},
            # steering：回合进行中用户又插了一条
            {"version": 2, "type": "message", "uuid": "u2", "turn_id": 1, "role": "user",
             "origin": {"kind": "human"}, "blocks": [{"type": "text", "text": "补充：顺便看一下 B"}]},
            {"version": 2, "type": "message", "uuid": "a2", "turn_id": 1, "role": "assistant",
             "origin": {"kind": "assistant"}, "blocks": [{"type": "text", "text": "A 和 B 都处理了"}]},
            {"version": 2, "type": "turn_end", "turn_id": 1, "first_uuid": "u1", "last_uuid": "a2"},
        ]
        write_session(path, events)
        turn = T.load_session(path).turns[0]

    print("=== A: 一个回合两条人类消息 ===")
    print(f"  user_text = {turn.user_text!r}")
    print()

    if "第一个问题" in turn.user_text and "顺便看一下 B" in turn.user_text:
        print("✔ 两条人类消息都保留在 user_text 里")
    else:
        failures += 1
        print("✖ user_text 丢了内容 —— 后一条覆盖了前一条（resume 后看不到）")
        if "第一个问题" not in turn.user_text:
            print("   缺：第一个问题")
        if "顺便看一下 B" not in turn.user_text:
            print("   缺：steering 插入的那条")

    # ── A2: 多条人类消息按 order 保留顺序 ───────────────────────────
    if "第一个问题" in turn.user_text and "顺便看一下 B" in turn.user_text:
        first = turn.user_text.index("第一个问题")
        second = turn.user_text.index("顺便看一下 B")
        if first < second:
            print("✔ 顺序正确（先原始提问，后 steering）")
        else:
            failures += 1
            print("✖ 顺序错了：steering 排到了原始提问前面")

    # ── A3: blocks 里两条人类消息都在 ───────────────────────────────
    human_blocks = [
        block for block in turn.blocks
        if block.get("kind") == "text" and "问题" in block.get("text", "")
    ]
    print(f"  blocks 里的人类文本块: {[b['text'] for b in human_blocks]}")
    # 有序投影把人类提问排除在外（它已由 user_text 表达），这本身是对的；
    # 这里只是记录事实，不做断言。

    # ── B: 对照，普通单提问回合不受影响 ─────────────────────────────
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "single.jsonl"
        events = base_events() + [
            {"version": 2, "type": "message", "uuid": "u1", "turn_id": 1, "role": "user",
             "origin": {"kind": "human"}, "blocks": [{"type": "text", "text": "唯一的问题"}]},
            {"version": 2, "type": "message", "uuid": "a1", "turn_id": 1, "role": "assistant",
             "origin": {"kind": "assistant"}, "blocks": [{"type": "text", "text": "回答"}]},
            {"version": 2, "type": "turn_end", "turn_id": 1, "first_uuid": "u1", "last_uuid": "a1"},
        ]
        write_session(path, events)
        single = T.load_session(path).turns[0]

    print()
    print("=== B: 对照，单提问回合 ===")
    print(f"  user_text = {single.user_text!r}")
    if single.user_text == "唯一的问题":
        print("✔ 对照行为不变")
    else:
        failures += 1
        print("✖ 对照回合被改坏了")

    # ── C: 带包裹标记的 steering（模型侧实际收到的形态） ────────────
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "wrapped.jsonl"
        wrapped = f"{WRAP_PREFIX}\n补充要求\n{WRAP_SUFFIX}"
        events = base_events() + [
            {"version": 2, "type": "message", "uuid": "u1", "turn_id": 1, "role": "user",
             "origin": {"kind": "human"}, "blocks": [{"type": "text", "text": "原始提问"}]},
            {"version": 2, "type": "message", "uuid": "u2", "turn_id": 1, "role": "user",
             "origin": {"kind": "human"}, "blocks": [{"type": "text", "text": wrapped}]},
            {"version": 2, "type": "message", "uuid": "a1", "turn_id": 1, "role": "assistant",
             "origin": {"kind": "assistant"}, "blocks": [{"type": "text", "text": "回答"}]},
            {"version": 2, "type": "turn_end", "turn_id": 1, "first_uuid": "u1", "last_uuid": "a1"},
        ]
        write_session(path, events)
        wrapped_turn = T.load_session(path).turns[0]

    print()
    print("=== C: steering 以包裹标记落盘时 ===")
    print(f"  user_text = {wrapped_turn.user_text!r}")
    if WRAP_PREFIX in wrapped_turn.user_text:
        print("  ℹ 包裹标记原样进了 user_text（UI 会显示成技术噪声，需另行处理）")

    print()
    print("✔ 全部通过" if failures == 0 else f"✖ {failures} 项失败")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
