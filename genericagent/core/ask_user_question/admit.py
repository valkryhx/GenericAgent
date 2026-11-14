"""ask_user_question 答案准入。

## 为什么需要它

用户在卡片上做的事，最终会变成 `updatedInput` 回传给工具。如果不校验，
这个通道就等于「任何能改 UI 的人都能改模型看到的题目」——
把 `options` 悄悄换掉、往 `answers` 里塞一个没问过的题、或者把预览内容调包。

CC 的做法是让工具自己实现 `admitCardAnswer`，由引擎在答案策略为 `strict`
时调用（调研文档 §3.1）。但 319 里**没有任何地方产出 `kind:"strict"`**，
交互式终端走的是 `launcher`，那条分支根本不过准入（调研 §3.1.0）。

GA 没有 `kind` 这个概念，所以**无条件准入**——这比 CC 严格，是刻意的：
CC 那套「准入挂在一条永不执行的分支上」是它自己的接线缺陷，不该复刻。
见实施方案 §5.1。

## 准入的四条规则

1. **顶层键白名单**：只允许 `answers` / `annotations` / `response` / `followUp`。
   出现 `questions` / `options` 等一律拒绝——那是「改题」，不是「答题」。
2. **每题白名单**：答案的 key 必须是已问过的题。不许新增题。
3. **长度与预算**：单个字符串有上限，答案总量有预算，防止把上下文撑爆。
4. **答案解析**：只接受字符串或字符串列表；嵌套对象、数字等一律拒绝。

空答案（`{}`）是允许的——用户可以一题都不答就提交（调研 §9.7.5）。
但**整个 payload 为空**不行，那等同于没提交。
"""

from __future__ import annotations

from typing import Any

# 单个答案字符串上限。对齐 CC 的 32768（`m0805:68` 的 `L`），
# 但 GA 的上下文更紧，这里取同量级而非逐字对齐。
MAX_STRING = 32768
MAX_NOTES = 8192
MAX_ANSWERS_TOTAL = 32768
# 答案总预算（含 annotations），防止「每题的答案都在上限附近」把上下文撑爆。
MAX_TOTAL_BUDGET = 48000

# 「只写了备注、没选答案」的哨兵值。对齐 CC 的 `(notes only)`（调研 §5.2）。
NOTES_ONLY = "(notes only)"

ALLOWED_KEYS = frozenset({"answers", "annotations", "response", "followUp"})


class AdmissionError(Exception):
    """准入失败。由调用方转成给模型的拒绝话术。"""


def _string_len(value: Any) -> int:
    if isinstance(value, str):
        return len(value)
    if isinstance(value, list):
        return sum(len(v) for v in value if isinstance(v, str))
    return 0


def admit(
    questions: list[dict], payload: Any
) -> tuple[bool, dict, str]:
    """校验卡片回传的 payload。

    返回 `(ok, normalized_payload, refusal_reason)`。
    `ok` 为 False 时 `refusal_reason` 是一句可以直接回给模型的话。
    """
    if not isinstance(payload, dict):
        return False, {}, "payload must be an object"

    unknown = sorted(set(payload) - ALLOWED_KEYS)
    if unknown:
        return False, {}, "answers may not carry %s" % ", ".join(unknown)

    if not payload:
        return False, {}, "empty submission: no answers and no response"

    asked_keys = {str(q.get("key") or "") for q in questions}
    asked_keys.discard("")

    answers = payload.get("answers", {})
    if not isinstance(answers, dict):
        return False, {}, "answers must be an object"

    normalized_answers: dict[str, Any] = {}
    total = 0

    for raw_key, value in answers.items():
        key = str(raw_key)
        if key not in asked_keys:
            return False, {}, "unknown answer key: %s" % key

        if isinstance(value, str):
            if len(value) > MAX_STRING:
                return False, {}, "answer for %s exceeds %d characters" % (key, MAX_STRING)
            total += len(value)
        elif isinstance(value, list):
            if not all(isinstance(v, str) for v in value):
                return False, {}, "answer for %s must be a string or a list of strings" % key
            size = sum(len(v) for v in value)
            if size > MAX_STRING:
                return False, {}, "answer for %s exceeds %d characters" % (key, MAX_STRING)
            total += size
        else:
            return False, {}, "answer for %s must be a string or a list of strings" % key

        normalized_answers[key] = value

    if total > MAX_ANSWERS_TOTAL:
        return False, {}, "total answer length exceeds %d characters" % MAX_ANSWERS_TOTAL

    annotations = payload.get("annotations", {})
    if not isinstance(annotations, dict):
        return False, {}, "annotations must be an object"

    for raw_key, value in annotations.items():
        key = str(raw_key)
        if key not in asked_keys:
            return False, {}, "unknown annotation key: %s" % key
        if not isinstance(value, dict):
            return False, {}, "annotation for %s must be an object" % key
        notes = value.get("notes")
        if notes is not None:
            if not isinstance(notes, str):
                return False, {}, "notes for %s must be a string" % key
            if len(notes) > MAX_NOTES:
                return False, {}, "notes for %s exceeds %d characters" % (key, MAX_NOTES)
            total += len(notes)

    if total > MAX_TOTAL_BUDGET:
        return False, {}, "total submission exceeds %d characters" % MAX_TOTAL_BUDGET

    normalized: dict[str, Any] = {"answers": normalized_answers}
    if annotations:
        normalized["annotations"] = {
            str(k): dict(v) for k, v in annotations.items()
        }
    for passthrough in ("response", "followUp"):
        if passthrough in payload:
            value = payload[passthrough]
            if not isinstance(value, str):
                return False, {}, "%s must be a string" % passthrough
            if len(value) > MAX_STRING:
                return False, {}, "%s exceeds %d characters" % (passthrough, MAX_STRING)
            normalized[passthrough] = value

    return True, normalized, ""


def finalize(questions: list[dict], payload: dict) -> dict:
    """第二道校验：把 payload 变成最终回给模型的形态。

    CC 只在 `strict` 分支上做这一步（`Vgi`），GA 无条件做。
    这里重新走一遍 `admit` 的核心约束——**即使调用方绕过了 `admit`，
    `finalize` 也会抛异常**。这是「不能只靠一道关」的落地（方案 §5.3）。
    """
    ok, normalized, reason = admit(questions, payload)
    if not ok:
        raise AdmissionError(reason)

    # 空串不算答过（对齐 CC 的 `!!qp(...)`，调研 §9.7.5）。
    answers = {
        k: v
        for k, v in normalized.get("answers", {}).items()
        if not (isinstance(v, str) and v == "")
    }

    out = dict(normalized)
    out["answers"] = answers
    return out


def unanswered_questions(questions: list[dict], payload: dict) -> list[str]:
    """返回没答的题 key，用于 `unanswered` 三态与告警。"""
    answers = payload.get("answers") or {}
    missing = []
    for q in questions:
        key = str(q.get("key") or "")
        if not key:
            continue
        value = answers.get(key)
        if value is None or value == "" or value == []:
            missing.append(key)
    return missing


def refusal_prompt(reason: str) -> str:
    """准入失败时回给模型的话。"""
    return (
        "ask_user_question rejected the answer: %s\n"
        "The user's answer may have been tampered with, or it did not match the "
        "questions you asked. Do not retry the same call blindly; either ask again "
        "with the same questions, or proceed under a stated assumption." % reason
    )


# 「Chat about this」出口的话术。
#
# CC 把「我不想选，我想跟你聊聊这个问题」实现成 `deny + feedback`，
# 而不是伪造一个答案（调研 §9.3）。这是整套机制里最值得抄的一条：
# 用户在选项之外有话要说，那是**新的输入**，不是对选项的选择。
#
# 措辞照 CC 的 `Nht`（`full:69562`）。
CHAT_ABOUT_THIS_PROMPT = (
    "The user wants to clarify these questions.\n"
    "This means they may have additional information, context or questions for you.\n"
    "Take their response into account and then reformulate the questions if appropriate.\n"
    "Start by asking them what they would like to clarify."
)
