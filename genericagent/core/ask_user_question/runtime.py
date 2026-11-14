"""阻塞式提问的等待与消息协议。

对标 `genericagent/core/permissions/runtime.py`，但载荷和语义不同：

| | PermissionRuntime | QuestionRuntime |
| --- | --- | --- |
| 载荷 | 工具名 + args 预览 | 完整题目 + 选项 |
| 回答 | accept / deny | 每题的答案（多选是列表） |
| headless | **deny**（fail-closed） | **unavailable**（问不了） |
| 校验 | 无 | 答案必须过 `admit` |

## headless 为什么不是 deny

「用户拒绝了」和「没人可问」对模型是两件事：

- 前者意味着「用户看过这个问题，并给出了否定的答复」——模型不该再问；
- 后者意味着「这个问题根本没送到人面前」——模型应该改用假设推进，或者报告需要人。

把这两者混为一谈（都用 deny）会让模型误判：它会以为自己问过、用户不答。
CC 在 headless 下干脆**不暴露工具**（`nee()`，调研 §11.8.1），
GA 保留工具但返回 `unavailable`——因为 GA 的工具列表是静态 schema，
按会话裁剪的改造成本更高，而一个明确的 `unavailable` 已经足够让模型正确决策。

见实施方案 §4.2。
"""

from __future__ import annotations

import threading
import time
import uuid
from concurrent.futures import Future, TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from typing import Any, Callable

from genericagent.core.ask_user_question.admit import admit

# 结果状态。不要用 True/False —— 三种结局必须能区分开。
SUBMITTED = "submitted"
CANCELLED = "cancelled"
UNAVAILABLE = "unavailable"

EmitFn = Callable[[dict], None]


@dataclass
class QuestionResult:
    """一次提问的结局。"""

    status: str
    answers: dict[str, Any] = field(default_factory=dict)
    annotations: dict[str, Any] = field(default_factory=dict)
    message: str = ""
    # 取消的原因："" = 用户按了 Esc；"chat" = 用户选了「Chat about this」。
    # 这两者给模型的话术不同（调研 §9.3）：后者是有话要说，不是不想答。
    reason: str = ""

    @property
    def answered(self) -> bool:
        return self.status == SUBMITTED


@dataclass
class _Pending:
    future: Future
    questions: list[dict]


class QuestionRuntime:
    """requestId → Future 的挂起表；线程安全。"""

    def __init__(self) -> None:
        self._pending: dict[str, _Pending] = {}
        self._emit: EmitFn | None = None
        self._lock = threading.Lock()

    # ---- emit 注入（与 PermissionRuntime 同名同语义，便于 bridge 复用） ----

    def set_emit(self, emit: EmitFn | None) -> None:
        """注入 bridge emit；None = headless（返回 unavailable，不是 deny）。"""
        self._emit = emit

    def has_emit(self) -> bool:
        return self._emit is not None

    # ---- 主流程 ----

    def ask(
        self,
        questions: list[dict],
        *,
        timeout_s: float | None = None,
        stop_check: Callable[[], bool] | None = None,
        poll_seconds: float = 0.1,
    ) -> QuestionResult:
        """发一次提问并阻塞等待。返回三种结局之一。

        `timeout_s` 为 None 表示一直等（由 `/stop` 或 `cancel` 结束）。
        """
        if self._emit is None:
            return QuestionResult(
                status=UNAVAILABLE,
                message=(
                    "No interactive UI is attached to this session, so the question "
                    "could not be shown to a user. Proceed under a stated assumption, "
                    "or report what you need from the user in your final message."
                ),
            )

        request_id = uuid.uuid4().hex
        fut: Future = Future()
        with self._lock:
            self._pending[request_id] = _Pending(future=fut, questions=questions)

        try:
            self._emit(
                {
                    "type": "question_request",
                    "requestId": request_id,
                    "questions": list(questions),
                }
            )
        except Exception as exc:  # emit 通道坏了，等同于没有 UI
            self._drop(request_id)
            return QuestionResult(
                status=UNAVAILABLE,
                message="Failed to deliver the question to the UI: %s" % exc,
            )

        deadline = None if timeout_s is None else time.monotonic() + timeout_s

        while True:
            if stop_check is not None and stop_check():
                self.cancel(request_id)
                break
            if deadline is not None and time.monotonic() > deadline:
                self.cancel(request_id)
                break
            try:
                fut.result(timeout=poll_seconds)
                break
            except FuturesTimeout:
                continue
            except Exception:
                self._drop(request_id)
                return QuestionResult(
                    status=CANCELLED, message="The question request failed."
                )

        if fut.done() and not fut.cancelled():
            try:
                settled = fut.result(timeout=0)
            except Exception:
                settled = None
            if isinstance(settled, QuestionResult):
                return settled

        self._drop(request_id)
        return QuestionResult(status=CANCELLED, message="The question was cancelled.")

    # ---- 结算 ----

    def submit(self, request_id: str, payload: Any) -> bool:
        """UI 回写答案。通过准入才生效；否则返回 False 且请求仍挂起。

        准入放在这里而不是调用方，是因为这是**唯一的入口**——
        前端、bridge、SDK 都从这条路回来，绕不过去（方案 §5.3）。
        """
        rid = str(request_id or "")
        with self._lock:
            pending = self._pending.get(rid)
        if pending is None or pending.future.done():
            return False

        ok, normalized, reason = admit(pending.questions, payload)
        if not ok:
            return False

        with self._lock:
            pending = self._pending.pop(rid, None)
        if pending is None or pending.future.done():
            return False

        pending.future.set_result(
            QuestionResult(
                status=SUBMITTED,
                answers=normalized.get("answers", {}),
                annotations=normalized.get("annotations", {}),
            )
        )
        self._emit_event("question_resolved", rid, "submitted")
        return True

    def cancel(self, request_id: str, reason: str = "") -> bool:
        rid = str(request_id or "")
        with self._lock:
            pending = self._pending.pop(rid, None)
        if pending is None or pending.future.done():
            return False
        pending.future.set_result(
            QuestionResult(
                status=CANCELLED,
                message="The question was cancelled.",
                reason=str(reason or ""),
            )
        )
        self._emit_event("question_resolved", rid, "cancelled")
        return True

    def cancel_all(self) -> int:
        """/stop：所有挂起的提问按 cancelled 收尾。"""
        with self._lock:
            items = list(self._pending.items())
            self._pending.clear()
        n = 0
        for rid, pending in items:
            if not pending.future.done():
                pending.future.set_result(
                    QuestionResult(status=CANCELLED, message="The question was cancelled.")
                )
                n += 1
            self._emit_event("question_resolved", rid, "cancelled")
        return n

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def pending_ids(self) -> list[str]:
        with self._lock:
            return list(self._pending)

    # ---- 内部 ----

    def _drop(self, request_id: str) -> None:
        with self._lock:
            self._pending.pop(request_id, None)

    def _emit_event(self, event_type: str, request_id: str, outcome: str) -> None:
        if self._emit is None:
            return
        try:
            self._emit(
                {"type": event_type, "requestId": request_id, "outcome": outcome}
            )
        except Exception:
            pass
