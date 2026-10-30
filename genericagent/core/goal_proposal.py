"""Model-proposed goals (Stage F, phase 4): ``propose_goal`` tool support.

Mirrors Claude Code 2.1.303 ``ProposeGoal`` (m1718/m0418): the *model* may
propose a session goal condition, but the user keeps control. Default path is a
one-keypress approval dialog; only when the user's own words already expressed
the outcome may the model set it directly (``ask_user=false``).

This module is pure/near-pure so the gate logic and condition normalization are
unit-testable without any network or UI. The blocking approval runtime mirrors
``genericagent.core.permissions.runtime.PermissionRuntime``: emit a request, wait
on a Future, fail-closed (deny) when no UI is attached.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from concurrent.futures import Future, TimeoutError as FuturesTimeout
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from genericagent.core.goal import GOAL_PROPOSAL_MAX, GOAL_CLEAR_WORDS, normalize_condition

# Settings values for ``modelProposedGoals`` (m0062). ``auto`` is the default:
# still show approval unless the model explicitly passes ask_user=false.
SETTING_AUTO = "auto"
SETTING_ALWAYS_ASK = "alwaysAsk"
SETTING_DISABLED = "disabled"
_SETTING_VALUES = {SETTING_AUTO, SETTING_ALWAYS_ASK, SETTING_DISABLED}

APPROVE = "approve"
REJECT = "reject"

EmitFn = Callable[[dict], None]


def _truthy(raw: object) -> bool:
    return str(raw or "").strip().lower() in {"1", "true", "yes", "on"}


def is_feature_enabled() -> bool:
    """Kill switch for the whole feature (CC ``tengu_propose_goal``).

    Deviation from CC: GA ships this ON by default (single-user local agent; the
    user asked for a working feature). ``GA_PROPOSE_GOAL_ENABLED=0/off`` disables.
    """
    raw = __import__("os").environ.get("GA_PROPOSE_GOAL_ENABLED")
    if raw is None:
        return True
    return _truthy(raw)


def _user_settings_path() -> Path:
    home = Path(__import__("os").environ.get("USERPROFILE") or __import__("os").environ.get("HOME") or Path.home())
    return home / ".genericagent" / "settings.json"


def read_model_proposed_goals_setting(*, settings_path: Path | None = None) -> str:
    """Return ``auto`` | ``alwaysAsk`` | ``disabled`` from *trusted* sources only.

    Trusted = env flag override + the user settings file. workspace project/local
    settings are intentionally ignored (consent-affecting, CC m0062). Unknown
    values fall back to ``auto``.
    """
    env = __import__("os").environ.get("GA_MODEL_PROPOSED_GOALS")
    if env is not None:
        text = str(env).strip()
        if text in _SETTING_VALUES:
            return text
    path = settings_path or _user_settings_path()
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeDecodeError):
        return SETTING_AUTO
    if not isinstance(data, dict):
        return SETTING_AUTO
    for key in ("modelProposedGoals", "model_proposed_goals"):
        value = data.get(key)
        if isinstance(value, str) and value.strip() in _SETTING_VALUES:
            return value.strip()
    return SETTING_AUTO


@dataclass(frozen=True)
class NormalizedProposal:
    ok: bool
    condition: str = ""
    error: str = ""


def normalize_proposal_condition(raw: object, *, max_len: int = GOAL_PROPOSAL_MAX) -> NormalizedProposal:
    """Collapse separators, strip, then validate a model-proposed condition."""
    text = normalize_condition(str(raw or ""))
    if not text:
        return NormalizedProposal(False, error="goal condition must not be empty")
    if len(text) > max_len:
        return NormalizedProposal(
            False,
            error="goal condition is limited to %d characters (got %d); keep it readable in the approval dialog"
            % (max_len, len(text)),
        )
    if text.lower() in GOAL_CLEAR_WORDS:
        return NormalizedProposal(
            False,
            error="propose_goal only proposes a goal; it cannot clear one (use /goal clear instead)",
        )
    return NormalizedProposal(True, condition=text)


def resolve_ask_user(setting: str, ask_user_arg: object) -> bool:
    """Whether the approval dialog is required for this proposal.

    ``alwaysAsk`` forces it; otherwise it is required unless the model passed
    ``ask_user`` explicitly false (meaning the user's words already stated it).
    """
    if setting == SETTING_ALWAYS_ASK:
        return True
    return ask_user_arg is not False


@dataclass
class ProposalRequest:
    condition: str
    request_id: str = ""

    def to_event(self) -> dict:
        return {
            "type": "goal_proposal_request",
            "requestId": self.request_id,
            "condition": self.condition,
        }


class GoalProposalRuntime:
    """requestId -> Future table for the one-keypress goal approval dialog.

    Fail-closed: with no emit (headless) the request is denied immediately.
    """

    def __init__(self) -> None:
        self._pending: dict[str, Future] = {}
        self._emit: EmitFn | None = None
        self._lock = threading.Lock()

    def set_emit(self, emit: EmitFn | None) -> None:
        self._emit = emit

    def has_emit(self) -> bool:
        return self._emit is not None

    def request_approval(self, condition: str, *, timeout: float = 300.0, poll_seconds: float = 0.1) -> bool:
        """Block until the user approves/rejects. Returns True only on approve."""
        if self._emit is None:
            return False
        request_id = uuid.uuid4().hex
        fut: Future = Future()
        with self._lock:
            self._pending[request_id] = fut
        try:
            self._emit(ProposalRequest(condition=condition, request_id=request_id).to_event())
        except Exception:
            self._drop(request_id, REJECT)
            return False
        import time as _time
        deadline = _time.time() + float(timeout)
        while not fut.done():
            if _time.time() >= deadline:
                self.cancel(request_id)
                return False
            try:
                return fut.result(timeout=poll_seconds) == APPROVE
            except FuturesTimeout:
                continue
            except Exception:
                return False
        try:
            return fut.result(timeout=0) == APPROVE
        except Exception:
            return False

    def resolve(self, request_id: str, decision: object) -> bool:
        decision_n = APPROVE if str(decision or "").strip().lower() in {"approve", "accept", "allow", "yes"} else REJECT
        with self._lock:
            fut = self._pending.pop(str(request_id or ""), None)
        if fut is None or fut.done():
            return False
        fut.set_result(decision_n)
        self._emit_settled(str(request_id or ""), "resolved")
        return True

    def cancel(self, request_id: str) -> bool:
        return self._drop(str(request_id or ""), REJECT)

    def cancel_all(self) -> int:
        with self._lock:
            items = list(self._pending.items())
            self._pending.clear()
        n = 0
        for req_id, fut in items:
            if not fut.done():
                fut.set_result(REJECT); n += 1
            self._emit_settled(req_id, "cancelled")
        return n

    def pending_count(self) -> int:
        with self._lock:
            return len(self._pending)

    def _drop(self, request_id: str, decision: str) -> bool:
        with self._lock:
            fut = self._pending.pop(request_id, None)
        if fut is None or fut.done():
            return False
        fut.set_result(decision)
        self._emit_settled(request_id, "cancelled")
        return True

    def _emit_settled(self, request_id: str, outcome: str) -> None:
        emit = self._emit
        if emit is None or not request_id:
            return
        try:
            emit({"type": "goal_proposal_settled", "requestId": request_id, "outcome": outcome})
        except Exception:
            pass
