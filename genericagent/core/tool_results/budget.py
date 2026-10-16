"""Bounded, durable tool-result rendering.

This is GA's small counterpart to Claude Code's generic tool-result budget
(``m0297_0xc5de28c.js`` / constants in ``m0089_0xc0a83b7.js``).  The rule is
not "drop everything large": a large result is first persisted in full, then a
bounded preview plus a readable path is returned to the model.  Only when the
write fails do we fall back to a real truncation.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path

from genericagent.core.redaction import redact_sensitive_text
from genericagent.core.tool_results.limits import get_tool_result_max_chars, normalize_tool_result_max_chars


DEFAULT_MAX_RESULT_CHARS = 50_000
DEFAULT_PREVIEW_CHARS = 2_000
DEFAULT_PERSIST_LIMIT_CHARS = 500_000
PERSISTED_OPEN = "<persisted-output>"
PERSISTED_CLOSE = "</persisted-output>"
TRUNCATED_OPEN = "<truncated-output>"
TRUNCATED_CLOSE = "</truncated-output>"
_EXEMPT_TOOLS = frozenset({"file_read", "code_run", "structured_output", "no_tool"})
_SAFE_SEGMENT_RE = re.compile(r"[^A-Za-z0-9._-]+")


def _env_int(name: str, default: int) -> int:
    raw = str(os.environ.get(name, "") or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _safe_segment(value: str, fallback: str = "result") -> str:
    text = _SAFE_SEGMENT_RE.sub("_", str(value or "").strip()).strip("._")
    return (text or fallback)[:120]


def _base_dir(handler) -> Path:
    for attr in ("tool_result_base_dir", "workspace_root", "cwd"):
        raw = getattr(handler, attr, None)
        if raw:
            candidate = Path(str(raw)).expanduser()
            if not candidate.is_absolute():
                candidate = Path.cwd() / candidate
            return candidate
    return Path.cwd()


def _relative_target(handler, tool_key: str, structured: bool) -> Path:
    raw_namespace = str(getattr(handler, "tool_result_namespace", "") or "session")
    namespace_parts = [_safe_segment(part, "session") for part in raw_namespace.replace("\\", "/").split("/") if part]
    namespace = Path(*namespace_parts) if namespace_parts else Path("session")
    key = _safe_segment(tool_key, "result")
    suffix = "json" if structured else "txt"
    return Path("tool-results") / namespace / f"{key}.{suffix}"


def _preview_text(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    cut = text[:limit]
    newline = cut.rfind("\n")
    if newline >= limit // 2:
        cut = cut[:newline]
    return cut.rstrip() + "\n..."


def _structured_preview(text: str, limit: int, ref: str) -> str:
    try:
        # Imported lazily: workflow_child_agent imports agent_loop, so a
        # module-level import here would create a cycle.
        from genericagent.workflow.child_agent import bounded_structured_summary

        return bounded_structured_summary(text, limit=limit, ref=ref)
    except Exception:
        return _preview_text(text, limit)


def _write_persisted(path: Path, content: str, *, structured: bool, persist_limit: int) -> tuple[bool, bool]:
    """Write content and return ``(succeeded, truncated_at_persist_limit)``."""
    path.parent.mkdir(parents=True, exist_ok=True)
    truncated = False
    payload = content
    if len(content) > persist_limit:
        truncated = True
        if structured:
            payload = _structured_preview(content, persist_limit, str(path))
        else:
            payload = content[:persist_limit]
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(payload)
    return True, truncated


def _envelope(
    *,
    persisted: bool,
    path: Path | None,
    relative_ref: str,
    original_chars: int,
    preview: str,
    sha256: str,
    truncated_at_limit: bool,
) -> str:
    if persisted and path is not None:
        message = (
            f"Output too large ({original_chars} chars). Full output saved to: {path}\n"
            f"artifactPath: {relative_ref}\n"
            f"originalChars: {original_chars}\n"
            f"truncated: false\n"
            f"sha256: {sha256}\n"
        )
        if truncated_at_limit:
            message = (
                f"Output exceeded the persist limit; only the first saved portion is in: {path}\n"
                f"artifactPath: {relative_ref}\n"
                f"originalChars: {original_chars}\n"
                f"truncated: true\n"
                f"sha256: {sha256}\n"
            )
        return (
            f"{PERSISTED_OPEN}\n"
            f"{message}"
            f"Preview:\n{preview}\n"
            f"{PERSISTED_CLOSE}"
        )
    return (
        f"{TRUNCATED_OPEN}\n"
        f"Output too large ({original_chars} chars). It could not be saved, so only the first "
        f"{len(preview)} characters are shown; the rest was dropped. If the tool can page or "
        f"filter its results, call it again for the part you need.\n"
        f"originalChars: {original_chars}\n"
        f"truncated: true\n\n"
        f"{preview}\n"
        f"{TRUNCATED_CLOSE}"
    )


def budget_tool_result(
    handler,
    tool_name: str,
    tool_key: str,
    content: str,
    *,
    structured: bool = False,
    max_chars: int | None = None,
) -> str:
    """Return content unchanged or a persisted/truncated envelope.

    ``max_chars`` is an optional per-call override.  When omitted, the
    per-tool registry populated by MCP discovery (and, later, ordinary tool
    metadata) is consulted before the global default.
    """
    name = str(tool_name or "")
    resolved_max_chars = normalize_tool_result_max_chars(max_chars)
    if resolved_max_chars is None:
        resolved_max_chars = get_tool_result_max_chars(name)
    if name in _EXEMPT_TOOLS and resolved_max_chars is None:
        return content
    max_chars = resolved_max_chars or _env_int("GA_TOOL_RESULT_MAX_CHARS", DEFAULT_MAX_RESULT_CHARS)
    preview_chars = _env_int("GA_TOOL_RESULT_PREVIEW_CHARS", DEFAULT_PREVIEW_CHARS)
    persist_limit = _env_int("GA_TOOL_RESULT_PERSIST_LIMIT_CHARS", DEFAULT_PERSIST_LIMIT_CHARS)
    text = str(content or "")
    if len(text) <= max_chars:
        return text

    original_chars = len(text)
    safe_text = redact_sensitive_text(text)
    digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
    target = _base_dir(handler) / _relative_target(handler, tool_key, structured)
    relative_ref = _relative_target(handler, tool_key, structured).as_posix()
    preview = _structured_preview(safe_text, preview_chars, relative_ref) if structured else _preview_text(safe_text, preview_chars)
    try:
        _ok, truncated_at_limit = _write_persisted(
            target,
            safe_text,
            structured=structured,
            persist_limit=persist_limit,
        )
    except (OSError, ValueError, TypeError):
        return _envelope(
            persisted=False,
            path=None,
            relative_ref=relative_ref,
            original_chars=original_chars,
            preview=preview,
            sha256=digest,
            truncated_at_limit=False,
        )
    return _envelope(
        persisted=True,
        path=target,
        relative_ref=relative_ref,
        original_chars=original_chars,
        preview=preview,
        sha256=digest,
        truncated_at_limit=truncated_at_limit,
    )
