"""Prompts typed while a turn is running, waiting for the next fold point.

Why this exists: the composer accepts text during a turn (the agent is busy, but the
input is not), and that text used to be dropped on the floor -- ``submit()`` answered
``busy`` and ``Enter`` was a no-op. Claude Code instead queues it and folds it into the
*running* turn at the next tool boundary, so the user's mid-turn correction reaches the
model without starting a second turn.

Design notes:

* A plain ordered store with no turn coupling. The queue outlives a turn on purpose: a
  message that arrives after the last fold point of turn N must survive into turn N+1,
  which is exactly the case a per-turn buffer gets wrong.
* ``drain()`` is atomic. The UI thread pushes while the turn thread drains, so the
  read-and-clear has to happen under one lock; a "read then clear" pair loses whatever
  arrives between the two.
* The wrapper text is CC's (``The user sent a new message while you were working:``).
  The model has to be able to tell this apart from the request it is already working
  on, or it re-answers the original question.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone


WRAP_PREFIX = "[GA_USER_MIDTURN_MESSAGE]"
WRAP_SUFFIX = "[/GA_USER_MIDTURN_MESSAGE]"

# Distinct enough from any user text to cut on. Kept as separate constants because
# `extract_steering_prompts()` must know exactly where the instruction frame starts —
# deriving it from the templates would mean parsing a format string.
_TEMPLATE_TAIL_ZH = "用户在上一轮执行过程中补充了这条要求"
_TURN_START_TAIL_ZH = "用户上次请求尚未收尾时补充了这些内容"
# The English half of the same tail sentence. Identical wording in both templates up to
# the em dash, so one marker covers the mid-turn case and the tail-start case cuts on
# its own lead-in above.
_TEMPLATE_TAIL_EN = "This message arrived mid-turn"

# Mirrors Claude Code's mid-turn wrapper (m0108 `QJe` / m0459 `z4n`). Kept verbatim in
# English so the wording stays comparable to CC's, with a Chinese line because GA's
# prompts are authored in Chinese and a bare English instruction inside a Chinese turn
# reads like stray data.
_TEMPLATE = (
    "The user sent a new message while you were working:\n"
    "{body}\n"
    "\n"
    "This message arrived mid-turn -- address it as you continue this turn, rather than "
    "treating it as a new conversation turn. 用户在上一轮执行过程中补充了这条要求，请在当前回合内一并处理。"
)

# A message that was still queued when the previous turn ended reaches the model at the
# *start of the next turn* instead. The mid-turn wording would be a lie there: there is
# no running turn to continue, and telling the model otherwise makes it answer the new
# message as a side note to the previous request.
_TURN_START_TEMPLATE = (
    "The user sent these messages while the previous turn was still finishing:\n"
    "{body}\n"
    "\n"
    "They were accepted but could not be folded into that turn, so they are part of your "
    "task now. 用户上次请求尚未收尾时补充了这些内容，未能并入那一回合，请在本回合一并处理。"
)


def _now_iso() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


@dataclass(frozen=True)
class QueuedPrompt:
    text: str
    source: str = "user"
    created_at: str = field(default_factory=_now_iso)


class PendingPromptQueue:
    """Ordered, thread-safe, drainable queue of mid-turn prompts."""

    def __init__(self) -> None:
        self._items: list[QueuedPrompt] = []
        self._lock = threading.Lock()

    def push(self, text, *, source: str = "user") -> bool:
        value = str(text or "").strip()
        if not value:
            # An empty submit is not a message. Returning False lets the caller tell
            # "queued" from "nothing to queue" instead of showing a badge for it.
            return False
        with self._lock:
            self._items.append(QueuedPrompt(text=value, source=source))
        return True

    def drain(self) -> list[QueuedPrompt]:
        with self._lock:
            items, self._items = self._items, []
        return items

    def clear(self) -> None:
        with self._lock:
            self._items = []

    def __len__(self) -> int:
        with self._lock:
            return len(self._items)


def render_queued_prompts(items, *, when: str = "mid_turn") -> str:
    """Render drained prompts as one model-facing block, or ``""`` when empty.

    ``when`` picks the wrapper: ``"mid_turn"`` for the fold point inside a running turn,
    ``"turn_start"`` for a message that outlived its turn and is being delivered at the
    start of the next one. Both exist because the queue deliberately survives a turn
    boundary -- rendering both cases with the mid-turn wording would tell the model it is
    still inside a turn that has already ended.
    """
    texts = [str(getattr(item, "text", item) or "").strip() for item in items]
    texts = [text for text in texts if text]
    if not texts:
        return ""
    body = "\n\n".join(texts)
    template = _TURN_START_TEMPLATE if str(when) == "turn_start" else _TEMPLATE
    return "\n".join([WRAP_PREFIX, template.format(body=body), WRAP_SUFFIX])


def extract_steering_prompts(text) -> list[str]:
    """Pull the user's own words back out of mid-turn wrapper blocks.

    A steering message never becomes a human `message` event in the transcript: the
    queue renders it into the *next* request as a wrapped block, so the only place it
    survives is inside some later message's text (in practice a tool_result message,
    concatenated with the working-memory preamble). Resuming used to show nothing at
    all, because the raw transcript text was fed straight to the UI — the user saw the
    agent answer a question whose question was invisible.

    Parsing the wrapper back out is the only way to recover it. The two templates are
    the fact source; the body sits on the line after the template's first line and runs
    to the closing marker.
    """
    value = str(text or "")
    if WRAP_PREFIX not in value:
        return []
    out = []
    cursor = 0
    while True:
        start = value.find(WRAP_PREFIX, cursor)
        if start < 0:
            break
        end = value.find(WRAP_SUFFIX, start)
        if end < 0:
            break
        block = value[start + len(WRAP_PREFIX):end]
        cursor = end + len(WRAP_SUFFIX)
        # Drop the English instruction frame. The user's text is the leading part of
        # the block, before the template's tail sentence. Cut on whichever marker comes
        # first — the templates differ in wording, and a miss leaves the agent's
        # instruction text sitting in the user's question.
        cut = len(block)
        for marker in (_TEMPLATE_TAIL_EN, _TEMPLATE_TAIL_ZH, _TURN_START_TAIL_ZH):
            index = block.find(marker)
            if index >= 0:
                cut = min(cut, index)
        block = block[:cut]
        lines = [line.rstrip() for line in block.strip().split("\n")]
        # Drop the template's lead-in line ("The user sent ... working:") and any
        # blank line it left behind.
        while lines and not lines[-1].strip():
            lines.pop()
        if lines and lines[0].lstrip().lower().startswith(
            ("the user sent a new message", "the user sent these messages")
        ):
            lines = lines[1:]
        prompt = "\n".join(lines).strip()
        if prompt:
            out.append(prompt)
    return out
