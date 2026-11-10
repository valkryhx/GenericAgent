"""Provider error classification shared by the LLM layer and compaction.

A context-overflow rejection must be *catchable*, not just text: the tool loop
needs to roll the turn back and retry after compacting. Everything else stays on
the old path (an ``!!!Error:`` text block), because retrying a rate limit or an
auth failure would only burn the turn.
"""

import re

_CONTEXT_OVERFLOW_RE = re.compile(
    r"context_length|maximum context length|context window exceeded|"
    r"prompt is too long|input is too long|too many tokens|"
    r"exceeds? the (maximum )?context|request entity too large|"
    r"reduce the length of the messages",
    re.IGNORECASE,
)


class ContextOverflowError(RuntimeError):
    """The upstream rejected the request because the prompt did not fit."""

    def __init__(self, message=""):
        super().__init__(message or "context overflow")
        self.provider_message = str(message or "")


def is_context_overflow_error(message) -> bool:
    """True when a provider error means "the prompt did not fit"."""
    return bool(_CONTEXT_OVERFLOW_RE.search(str(message or "")))
