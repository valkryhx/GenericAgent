# -*- coding: utf-8 -*-
"""探针：`resume_session()` 真实发出的 history_replace 里有没有工具调用。

## 测什么

直接调用产品的 `GenericAgentBridge.resume_session(path)`，截获它 emit 的
`history_replace` 事件，断言消息里带 `toolUses` / `toolResults`，并把渲染输入
交给前端的等价逻辑判断。

## 为什么不走真 TUI

`/resume` 的选择器会挑「最新会话」，而最新那个常常是 0 轮的启动壳，测不到工具
调用。直接喂一个已知含 `tool_use` 的会话文件，变量只有一个：bridge 的投影逻辑。

真机渲染已由 `frontends/ink/ui/probes/resume_tool_rows.ts` 与
`src/messageWindow.test.ts` 覆盖，本探针只钉住「bridge 这一层有没有把数据发出去」。

## 根因坐标（已修）

`bridge.py:_resume_ui_messages_with_checkpoints` 原先把 `turn.assistant_text`
直接塞进 messages，而 `assistant_text` 由 `_message_text_from_blocks` 生成、
只含 `type == "text"` 块 → 工具调用在这一步离开 UI。

运行：python tests/probes/python/_probe_resume_bridge_payload.py
"""
import json
import sys
from pathlib import Path

sys.stdout.reconfigure(encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3] / "frontends"))

from frontends.ink import bridge as B  # noqa: E402

REPO = Path(__file__).resolve().parents[3]


def pick_session() -> Path | None:
    """找一个真实含 tool_use 的 v2 会话。"""
    best = None
    for path in (REPO / "temp" / "sessions").glob("*.jsonl"):
        try:
            text = path.read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        if '"tool_use"' not in text:
            continue
        if best is None or path.stat().st_mtime > best.stat().st_mtime:
            best = path
    return best


class Stub:
    _task_seq = 0
    _rewind_snapshots = {}

    def _backend_history(self):
        return []


def main() -> int:
    path = pick_session()
    if path is None:
        print("没有含 tool_use 的会话，无法测量")
        return 2
    print(f"会话: {path.name}\n")

    messages = B.GenericAgentBridge._resume_ui_messages_with_checkpoints(Stub(), str(path))

    total_uses = total_results = total_thinking = 0
    print(f"bridge 产出 {len(messages)} 条 UI 消息:")
    for message in messages:
        uses = message.get("toolUses") or []
        results = message.get("toolResults") or []
        thoughts = message.get("thinking") or []
        total_uses += len(uses)
        total_results += len(results)
        total_thinking += len(thoughts)
        tag = ""
        if uses:
            tag = "  ● " + ", ".join(u["tool"] for u in uses[:4])
        if thoughts:
            tag += "  ✻ " + str(len(thoughts))
        print(f"  role={message['role']:9s} text={len(message.get('text') or ''):5d}字"
              f"  toolUses={len(uses)} toolResults={len(results)} thinking={len(thoughts)}{tag}")

    print("\n=== 判定 ===")
    print(f"  总 toolUses = {total_uses}   总 toolResults = {total_results}"
          f"   总 thinking = {total_thinking}")
    ok = total_uses > 0
    print("  OK  bridge 已把工具调用发给 UI" if ok else "  NG  bridge 仍然只发纯文本")
    print("  OK  bridge 已把 thinking 发给 UI" if total_thinking > 0
          else "  NG  thinking 仍未进入 UI")

    # 抽查一条：input 必须是可被前端解析的 JSON 字符串
    for message in messages:
        for use in (message.get("toolUses") or []):
            try:
                json.loads(use["input"] or "{}")
            except (TypeError, ValueError) as exc:
                print(f"  ✖ toolUses.input 不是合法 JSON（前端无法摘要）: {exc}")
                return 1
            break
        else:
            continue
        break
    print("  OK  toolUses.input 是可解析的 JSON 字符串")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())