"""Per-tool result-size limits shared by tool runtimes and the budget layer.

The budget renderer stays generic: callers or runtimes can register a
``maxResultSizeChars`` value for a tool name, and ``budget_tool_result`` will
consult this registry automatically.  This keeps future ordinary-tool support
from requiring new ``if tool_name == ...`` branches in the budget core.
"""

from __future__ import annotations

import math
import threading
from typing import Mapping


_LOCK = threading.RLock()
_LIMITS: dict[str, int] = {}


def normalize_tool_result_max_chars(value: object) -> int | None:
    """Return a positive integer character limit, or ``None`` if invalid."""
    if isinstance(value, bool):
        return None
    if not isinstance(value, (int, float)):
        return None
    try:
        numeric = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(numeric) or numeric <= 0:
        return None
    return max(1, int(math.floor(numeric)))


def set_tool_result_max_chars(tool_name: str, value: object) -> int | None:
    """Set or clear the per-tool limit for ``tool_name``."""
    name = str(tool_name or "")
    normalized = normalize_tool_result_max_chars(value)
    with _LOCK:
        if not name or normalized is None:
            _LIMITS.pop(name, None)
        else:
            _LIMITS[name] = normalized
    return normalized


def get_tool_result_max_chars(tool_name: str) -> int | None:
    """Return the registered limit for ``tool_name``, if any."""
    with _LOCK:
        return _LIMITS.get(str(tool_name or ""))


def replace_tool_result_max_chars(
    limits: Mapping[str, object] | None,
    *,
    prefix: str = "",
) -> None:
    """Replace limits, optionally only for names matching ``prefix``."""
    normalized: dict[str, int] = {}
    for name, value in (limits or {}).items():
        item = normalize_tool_result_max_chars(value)
        key = str(name)
        if item is not None and (not prefix or key.startswith(prefix)):
            normalized[key] = item
    with _LOCK:
        if prefix:
            for name in [key for key in _LIMITS if key.startswith(prefix)]:
                del _LIMITS[name]
        else:
            _LIMITS.clear()
        _LIMITS.update(normalized)


def clear_tool_result_max_chars(*, prefix: str = "") -> None:
    """Clear all limits, or only those whose names start with ``prefix``."""
    with _LOCK:
        if not prefix:
            _LIMITS.clear()
            return
        for name in [key for key in _LIMITS if key.startswith(prefix)]:
            del _LIMITS[name]
