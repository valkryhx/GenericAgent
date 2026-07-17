from __future__ import annotations

import copy
import json
import os
import re
import time
from dataclasses import dataclass
from typing import Any, Callable

import token_meter


SummarizeFn = Callable[[str, str], str]

DEFAULT_CONTEXT_WIN = 400_000
MAX_COMPACT_SOURCE_CHARS = 800_000

# 软线兜底比率：直接喂 legacy cfg（无 auto_compact_tokens）的老路径，从 context_win
# 派生软线阈值时用。对齐 Codex 90%。
_AUTO_COMPACT_RATIO_FALLBACK = 0.90


@dataclass
class CompactResult:
    ok: bool
    message: str
    summary: str = ""
    original_messages: int = 0
    compacted_messages: int = 0


def _soft_limit_tokens(backend: Any) -> int:
    """软线（摘要式压缩触发）token 阈值。

    优先读 backend.auto_compact_tokens（配置层派生或 Session 兜底）；缺失时从
    context_win 按 0.90 兜底派生（直接喂 legacy cfg 的老路径）。
    """
    explicit = getattr(backend, "auto_compact_tokens", None)
    if explicit:
        return int(explicit)
    context_win = int(getattr(backend, "context_win", DEFAULT_CONTEXT_WIN) or DEFAULT_CONTEXT_WIN)
    return round(context_win * _AUTO_COMPACT_RATIO_FALLBACK)


def should_auto_compact_agent(agent: Any, pending_text: str = "") -> bool:
    """软线判断：估算「当前上下文 + 本次待发 pending_text」的 token 是否超软线阈值。

    token 化后（对齐 Codex/CC）：当前 token 由 token_meter.estimate_context_tokens
    给出（真实 usage 基准 + 增量估算，冷启动全量估算）；pending_text 是本次还没进
    history 的新输入，按兜底比率折成 token 计入。阈值用软线 auto_compact_tokens。
    """
    backend = _backend(agent)
    if backend is None:
        return False
    # 总开关关闭（对齐 Claude Code DISABLE_AUTO_COMPACT）：不自动摘要。手动 /compact 不走这里。
    if not bool(getattr(backend, "auto_compact_enabled", True)):
        return False
    history = getattr(backend, "history", []) or []
    last_usage = getattr(backend, "last_usage_tokens", None)

    current = token_meter.estimate_context_tokens(history, last_usage)
    pending_tokens = int(len(str(pending_text or "")) / token_meter.CHARS_PER_TOKEN_FALLBACK)
    return current + pending_tokens > _soft_limit_tokens(backend)


def compact_agent_context(
    agent: Any,
    instructions: str = "",
    summarize_fn: SummarizeFn | None = None,
) -> CompactResult:
    backend = _backend(agent)
    if backend is None or not hasattr(backend, "history"):
        return CompactResult(False, "No backend history available.")
    old_backend_history = copy.deepcopy(getattr(backend, "history", []) or [])
    old_agent_history = copy.deepcopy(getattr(agent, "history", []) or [])
    client = getattr(agent, "llmclient", None)
    old_last_tools = copy.deepcopy(getattr(client, "last_tools", "")) if client is not None else ""
    if not old_backend_history:
        return CompactResult(False, "No conversation history to compact.")

    try:
        source = _history_to_text(old_backend_history, _source_char_budget(backend))
        summarizer = summarize_fn or (lambda src, ins: _summarize_with_backend(backend, src, ins))
        summary = _clean_summary(summarizer(source, instructions))
        if not summary:
            raise RuntimeError("compact summary is empty")
        compact_history = _summary_history_pair(summary)
        backend.history = compact_history
        if hasattr(agent, "history"):
            agent.history = [f"[Agent] Compacted context: {_one_line(summary, 160)}"]
        if client is not None and hasattr(client, "last_tools"):
            client.last_tools = ""
        if hasattr(agent, "handler"):
            agent.handler = None
        return CompactResult(
            True,
            f"Compacted {len(old_backend_history)} messages into summary context.",
            summary=summary,
            original_messages=len(old_backend_history),
            compacted_messages=len(compact_history),
        )
    except Exception as exc:
        backend.history = old_backend_history
        if hasattr(agent, "history"):
            agent.history = old_agent_history
        if client is not None and hasattr(client, "last_tools"):
            client.last_tools = old_last_tools
        return CompactResult(False, _exception_message(exc))


def _backend(agent: Any) -> Any:
    return getattr(getattr(agent, "llmclient", None), "backend", None)


def _source_char_budget(backend: Any) -> int:
    context_win = int(getattr(backend, "context_win", DEFAULT_CONTEXT_WIN) or DEFAULT_CONTEXT_WIN)
    return max(4000, min(context_win * 2, MAX_COMPACT_SOURCE_CHARS))


def _history_to_text(history: list[dict[str, Any]], budget: int) -> str:
    lines: list[str] = []
    for idx, msg in enumerate(history or [], 1):
        role = str(msg.get("role", "unknown")).upper()
        text = _content_to_text(msg.get("content"))
        lines.append(f"## Message {idx} - {role}\n{_middle_truncate(text, 6000)}")
    source = "\n\n".join(lines)
    return _middle_truncate(source, budget)


def _content_to_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for block in content:
            if not isinstance(block, dict):
                parts.append(str(block))
                continue
            typ = block.get("type")
            if typ == "text":
                parts.append(str(block.get("text", "")))
            elif typ == "tool_result":
                parts.append(f"<tool_result>{_content_to_text(block.get('content', ''))}</tool_result>")
            elif typ == "tool_use":
                parts.append(f"<tool_use>{json.dumps(block.get('input', {}), ensure_ascii=False)}</tool_use>")
            elif typ == "thinking":
                parts.append("<thinking>[omitted]</thinking>")
            else:
                parts.append(json.dumps(block, ensure_ascii=False, default=str))
        return "\n".join(p for p in parts if p)
    return json.dumps(content, ensure_ascii=False, default=str)


def _middle_truncate(text: str, limit: int) -> str:
    text = str(text or "")
    if len(text) <= limit:
        return text
    half = max(1, limit // 2)
    return text[:half] + "\n...[truncated for compact]...\n" + text[-half:]


def _clean_summary(summary: str) -> str:
    summary = str(summary or "").strip()
    summary = re.sub(r"^```(?:\w+)?\s*|\s*```$", "", summary).strip()
    summary = re.sub(r"</?compact_summary>", "", summary, flags=re.IGNORECASE).strip()
    return summary


def _one_line(text: str, limit: int) -> str:
    text = re.sub(r"\s+", " ", str(text or "")).strip()
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _exception_message(exc: Exception) -> str:
    return str(exc) or type(exc).__name__


def replace_log_with_compact_history(log_path: str | None, compact_history: list[dict[str, Any]]) -> str | None:
    if not log_path:
        return None
    if len(compact_history or []) < 2:
        return None
    os.makedirs(os.path.dirname(os.path.abspath(log_path)), exist_ok=True)
    snapshot = None
    if os.path.isfile(log_path):
        with open(log_path, encoding="utf-8", errors="replace") as fh:
            old = fh.read()
        if "=== Prompt ===" in old and "=== Response ===" in old:
            stamp = time.strftime("%Y%m%d_%H%M%S")
            snapshot = os.path.join(
                os.path.dirname(os.path.abspath(log_path)),
                f"model_responses_snapshot_compact_{stamp}_{time.time_ns() % 1_000_000_000:09d}.txt",
            )
            with open(snapshot, "w", encoding="utf-8", errors="replace") as fh:
                fh.write(old)
    prompt = json.dumps(compact_history[0], ensure_ascii=False, indent=2)
    response = repr(compact_history[1].get("content", []))
    with open(log_path, "w", encoding="utf-8", errors="replace") as fh:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        fh.write(f"=== Prompt === {ts}\n{prompt}\n\n")
        fh.write(f"=== Response === {ts}\n{response}\n\n")
    return snapshot


def _summary_history_pair(summary: str) -> list[dict[str, Any]]:
    text = (
        "The previous conversation has been compacted. Continue from this context.\n"
        "<compact_summary>\n"
        f"{summary}\n"
        "</compact_summary>"
    )
    return [
        {"role": "user", "content": [{"type": "text", "text": text}]},
        {"role": "assistant", "content": [{"type": "text", "text": "<summary>loaded compact context</summary>"}]},
    ]


def _summarize_with_backend(backend: Any, source: str, instructions: str) -> str:
    prompt = _compact_prompt(source, instructions)
    old_history = copy.deepcopy(getattr(backend, "history", []) or [])
    try:
        backend.history = []
        ask_arg: Any = {"role": "user", "content": [{"type": "text", "text": prompt}]} if _is_native_backend(backend) else prompt
        return "".join(str(chunk) for chunk in backend.ask(ask_arg))
    finally:
        backend.history = old_history


def _is_native_backend(backend: Any) -> bool:
    candidates = [backend, getattr(backend, "primary", None)]
    return any(candidate is not None and "Native" in type(candidate).__name__ for candidate in candidates)


def _compact_prompt(source: str, instructions: str) -> str:
    extra = f"\n\nUser compact instructions:\n{instructions.strip()}" if instructions.strip() else ""
    return (
        "Summarize the previous GenericAgent conversation so the agent can continue without the original long context.\n"
        "Keep: user goal, current status, decisions, constraints, important files/paths, tool results, unresolved tasks, and next steps.\n"
        "Do not include filler. Do not call tools. Return only the compact summary text."
        f"{extra}\n\nConversation to compact:\n{source}"
    )
