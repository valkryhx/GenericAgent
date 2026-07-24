"""主 agent ask 档阻塞审批运行时。

策略（permission_policy）只返回 allow|ask|deny；本模块负责：
- 无 emit（headless）→ deny（fail-closed）
- 有 emit → 发 permission_request，Future 挂起，等 permission_response
- /stop / cancel_all → 所有 pending 按 deny 收尾

P1 仅 accept|deny；不做 session/always。
"""

from __future__ import annotations

import json
import threading
import uuid
from concurrent.futures import Future, TimeoutError as FuturesTimeout
from typing import Any, Callable

ACCEPT = "accept"
DENY = "deny"
DECISIONS = frozenset({ACCEPT, DENY})

EmitFn = Callable[[dict], None]


def format_args_preview(args: dict | None, *, max_len: int = 240) -> str:
    """把工具参数压成短文本，避免把整文件 content 塞进协议。"""
    if not args:
        return ""
    clean = {k: v for k, v in dict(args).items() if not str(k).startswith("_")}
    try:
        text = json.dumps(clean, ensure_ascii=False, default=str, separators=(",", ":"))
    except Exception:
        text = str(clean)
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def normalize_decision(decision: object) -> str:
    text = str(decision or "").strip().lower()
    if text in ("accept", "allow", "allow_once", "approved", "yes"):
        return ACCEPT
    return DENY


class PermissionRuntime:
    """requestId → Future 的挂起表；线程安全。"""

    def __init__(self) -> None:
        self._pending: dict[str, Future] = {}
        self._emit: EmitFn | None = None
        self._lock = threading.Lock()

    def set_emit(self, emit: EmitFn | None) -> None:
        """注入 bridge emit；None = headless fail-closed。"""
        self._emit = emit

    def has_emit(self) -> bool:
        return self._emit is not None

    def wait_for_decision(
        self,
        tool_name: str,
        args: dict | None,
        reason: str,
        *,
        stop_check: Callable[[], bool] | None = None,
        poll_seconds: float = 0.1,
    ) -> str:
        """阻塞直到 accept/deny。返回 ACCEPT 或 DENY。"""
        if self._emit is None:
            return DENY

        request_id = uuid.uuid4().hex
        fut: Future = Future()
        with self._lock:
            self._pending[request_id] = fut

        preview = format_args_preview(args)
        try:
            self._emit(
                {
                    "type": "permission_request",
                    "requestId": request_id,
                    "toolName": str(tool_name or ""),
                    "argsPreview": preview,
                    "reason": str(reason or ""),
                    "mode": "ask",
                }
            )
        except Exception:
            self._drop(request_id, DENY)
            return DENY

        while not fut.done():
            if stop_check and stop_check():
                self.cancel(request_id)
                return DENY
            try:
                return normalize_decision(fut.result(timeout=poll_seconds))
            except FuturesTimeout:
                continue
            except Exception:
                return DENY

        try:
            return normalize_decision(fut.result(timeout=0))
        except Exception:
            return DENY

    def resolve(self, request_id: str, decision: object) -> bool:
        """UI/bridge 回写决策。只生效一次。"""
        decision_n = normalize_decision(decision)
        with self._lock:
            fut = self._pending.pop(str(request_id or ""), None)
        if fut is None or fut.done():
            return False
        fut.set_result(decision_n)
        self._emit_settled(str(request_id or ""), "resolved")
        return True

    def cancel(self, request_id: str) -> bool:
        return self._drop(str(request_id or ""), DENY, outcome="cancelled")

    def cancel_all(self) -> int:
        """/stop：所有 pending 按 deny 收尾。返回取消条数。"""
        with self._lock:
            items = list(self._pending.items())
            self._pending.clear()
        n = 0
        for req_id, fut in items:
            if not fut.done():
                fut.set_result(DENY)
                n += 1
            self._emit_settled(req_id, "cancelled")
        return n

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def _drop(self, request_id: str, decision: str, *, outcome: str = "cancelled") -> bool:
        with self._lock:
            fut = self._pending.pop(request_id, None)
        if fut is None or fut.done():
            return False
        fut.set_result(decision)
        self._emit_settled(request_id, outcome)
        return True

    def _emit_settled(self, request_id: str, outcome: str) -> None:
        emit = self._emit
        if emit is None or not request_id:
            return
        try:
            emit(
                {
                    "type": "permission_request_settled",
                    "requestId": request_id,
                    "outcome": outcome,
                }
            )
        except Exception:
            pass
