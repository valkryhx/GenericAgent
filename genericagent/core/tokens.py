"""
GA token 计量层 —— 唯一的上下文 token 估算入口。

对标 Claude Code `utils/tokens.ts` 与 Codex `context_manager/history.rs` 的做法：
两者都**没有本地 tokenizer**（全仓无 tiktoken），而是用同一个模式——
**真实 API token 为基准 + 未发送增量的本地字符估算**：

  - 权威 token 只在 API 响应回来时才有（`sess.last_usage_tokens`，由 llmcore
    `_record_usage` 三种 wire 归一化后写入）。它覆盖「已发给模型的整段历史」，
    是准确的、且是绝大多数。
  - 压缩/裁剪发生在两次 API 调用**之间**，此刻精确 token 物理上拿不到。所以对
    「上次响应之后新增、还没发出去」的一小段消息做廉价字符估算补上。总误差可控。

这取代 GA 旧的「len(json.dumps(msg)) × 3」满盘字符估算——旧法从不使用手里已有的
真实 token，对中文（1 token≈1.5 字符）和代码/JSON（引号括号虚增字符）误差都很大。

本模块是纯函数、零重依赖，可独立单测；llmcore 与 compact_context 都调它。
"""

from __future__ import annotations

import json
from typing import Any, Optional

# 冷启动（从未成功请求过、无真实 usage）时的字符/token 兜底比率。
# 对齐 Codex(APPROX_BYTES_PER_TOKEN=4) 与 Claude Code(chars/4) 的 4。
CHARS_PER_TOKEN_FALLBACK = 4.0

# 单张图片块的固定 token 估算。base64 数据长度动辄数十万字符，绝不能按字符折算，
# 否则单张图就把估算撑爆。对齐 Claude Code 对 image/document 块固定计数的做法。
IMAGE_BLOCK_TOKENS = 1500


def real_total_tokens(last_usage: Any) -> Optional[int]:
    """从 `last_usage_tokens`（llmcore 归一化后的 dict）取真实上下文 token 总数。

    优先 total_tokens；缺失时由 input+output 求和。无有效值返回 None，调用方据此
    回退到全量字符估算（冷启动）。
    """
    if not isinstance(last_usage, dict):
        return None
    total = last_usage.get("total_tokens")
    if isinstance(total, (int, float)) and total > 0:
        return int(total)
    inp = last_usage.get("input_tokens") or 0
    out = last_usage.get("output_tokens") or 0
    if inp or out:
        return int(inp) + int(out)
    return None


def _msg_chars(msg: dict[str, Any]) -> int:
    """一条消息的「计费字符数」，图片块以固定 token 折回字符（避免 base64 爆炸）。

    图片块本身不贡献字符，改由 estimate_msg_tokens 单独加固定 token；这里只统计
    文本类内容的字符数。
    """
    content = msg.get("content") if isinstance(msg, dict) else None
    if isinstance(content, str):
        return len(content)
    if not isinstance(content, list):
        # 非常规结构，整体 json 序列化兜底。
        return len(json.dumps(content, ensure_ascii=False, default=str))
    chars = 0
    for block in content:
        if not isinstance(block, dict):
            chars += len(str(block))
            continue
        btype = block.get("type")
        if btype in ("image", "image_url", "input_image"):
            continue  # 图片单独按 IMAGE_BLOCK_TOKENS 计，不算字符
        if btype == "text":
            chars += len(str(block.get("text", "")))
        elif btype == "tool_use":
            chars += len(json.dumps(block.get("input", {}), ensure_ascii=False, default=str))
            chars += len(str(block.get("name", "")))
        elif btype == "tool_result":
            tc = block.get("content", "")
            if isinstance(tc, list):
                chars += sum(
                    len(str(b.get("text", "")))
                    for b in tc if isinstance(b, dict)
                )
            else:
                chars += len(str(tc))
        elif btype == "thinking":
            chars += len(str(block.get("thinking", "")))
        else:
            chars += len(json.dumps(block, ensure_ascii=False, default=str))
    return chars


def _msg_image_count(msg: dict[str, Any]) -> int:
    content = msg.get("content") if isinstance(msg, dict) else None
    if not isinstance(content, list):
        return 0
    return sum(
        1 for b in content
        if isinstance(b, dict) and b.get("type") in ("image", "image_url", "input_image")
    )


def estimate_msg_tokens(msg: dict[str, Any], chars_per_token: float = CHARS_PER_TOKEN_FALLBACK) -> int:
    """单条消息估算 token = 文本字符 / chars_per_token + 图片固定 token。"""
    cpt = chars_per_token if chars_per_token and chars_per_token > 0 else CHARS_PER_TOKEN_FALLBACK
    text_tokens = _msg_chars(msg) / cpt
    img_tokens = _msg_image_count(msg) * IMAGE_BLOCK_TOKENS
    return int(text_tokens + img_tokens)


def _total_chars(history: list[dict[str, Any]]) -> int:
    return sum(_msg_chars(m) for m in history or [])


def calibrated_cpt(history: list[dict[str, Any]], last_usage: Any) -> float:
    """从真实数据校准本会话的字符/token 比率。

    = 整段历史文本字符数 / 上次真实 total_token（扣掉图片占的 token 后）。
    中文会话实测可能约 1.6，代码/JSON 可能约 3.5，自适应而非固定 3。
    无真实 token（冷启动）时返回兜底 4。
    """
    real = real_total_tokens(last_usage)
    if real is None or real <= 0:
        return CHARS_PER_TOKEN_FALLBACK
    # 真实 total 里含图片 token；先减去图片估算，剩下才对应文本字符。
    img_tokens = sum(_msg_image_count(m) for m in history or []) * IMAGE_BLOCK_TOKENS
    text_real = real - img_tokens
    total_chars = _total_chars(history)
    if text_real <= 0 or total_chars <= 0:
        return CHARS_PER_TOKEN_FALLBACK
    cpt = total_chars / text_real
    # 夹在合理区间，防止异常数据（比如某轮 usage 明显偏小）把比率算飞。
    if cpt < 1.0:
        return 1.0
    if cpt > 8.0:
        return 8.0
    return cpt


def _last_assistant_index(history: list[dict[str, Any]]) -> int:
    """从后往前找最后一条 role==assistant 的下标；找不到返回 -1。

    它标记「上次 API 响应」的边界：其后的消息（新 user 输入、tool_result）是本次
    还没发给模型、未被真实 usage 统计的增量。对齐 Codex 的
    `items_after_last_model_generated_item`。
    """
    for i in range(len(history) - 1, -1, -1):
        m = history[i]
        if isinstance(m, dict) and m.get("role") == "assistant":
            return i
    return -1


def estimate_context_tokens(history: list[dict[str, Any]], last_usage: Any = None) -> int:
    """估算当前 history 的上下文 token 总数。

    有真实 usage：真实基准（覆盖到最后一条 assistant 为止的历史）+ 其后新增消息的
    字符估算增量。这是 Codex/CC 的核心公式。
    无真实 usage（冷启动）：全量字符估算兜底。
    """
    history = history or []
    real = real_total_tokens(last_usage)
    if real is None:
        # 冷启动：全量估算。用兜底比率。
        cpt = CHARS_PER_TOKEN_FALLBACK
        return sum(estimate_msg_tokens(m, cpt) for m in history)

    # 有真值：基准 + 「最后一条 assistant 之后」的增量估算。
    cut = _last_assistant_index(history)
    if cut < 0:
        # 有 usage 但历史里没有 assistant（异常/被清过）——退化为全量估算，
        # 用校准比率而非兜底，尽量贴近真值。
        cpt = calibrated_cpt(history, last_usage)
        return sum(estimate_msg_tokens(m, cpt) for m in history)

    cpt = calibrated_cpt(history, last_usage)
    delta = sum(estimate_msg_tokens(history[i], cpt) for i in range(cut + 1, len(history)))
    return real + delta
