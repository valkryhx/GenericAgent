from __future__ import annotations

import copy
import re
from typing import Any

REDACTION = "[REDACTED]"

SENSITIVE_KEY_RE = re.compile(
    r"(?i)(^|[_\-\s])(?:password|passwd|pwd|secret|token|access[_\-]?token|refresh[_\-]?token|api[_\-]?key|apikey|x[_\-]?api[_\-]?key|authorization|cookie|set[_\-]?cookie|private[_\-]?key|credential|client[_\-]?secret|tavilyapikey)(?:$|[_\-\s])"
)

TEXT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (
        re.compile(r"(?i)\b(Authorization\s*[:=]\s*Bearer\s+)([^\[\]\s,;}\)]+)"),
        rf"\1{REDACTION}",
    ),
    (
        re.compile(r"(?i)\b(Bearer\s+)([^\[\]\s,;}\)]+)"),
        rf"\1{REDACTION}",
    ),
    (
        re.compile(r"(?i)\b(x-api-key\s*[:=]\s*['\"]?)([^\[\]'\"\s,;}\)]+)"),
        rf"\1{REDACTION}",
    ),
    (
        re.compile(r"(?i)\b((?:set-cookie|cookie)\s*[:=]\s*)([^\[\]\s;,}]+)"),
        rf"\1{REDACTION}",
    ),
    (
        re.compile(r"(?i)([?&][^=\s&]*(?:api[_-]?key|token|secret|password|apikey|tavilyapikey)[^=\s&]*=)([^&\s]+)"),
        rf"\1{REDACTION}",
    ),
    (
        re.compile(r"(?i)(['\"]?(?:api[_-]?key|apikey|password|client[_-]?secret|access[_-]?token|refresh[_-]?token|(?<!forbidden\s)token|secret)['\"]?\s*[:=]\s*['\"]?)([^'\"\s,;}\]\)]+)"),
        rf"\1{REDACTION}",
    ),
    (
        re.compile(r"(?i)\b(jwt\s*[:=]\s*['\"]?)([A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)"),
        rf"\1{REDACTION}",
    ),
    (
        re.compile(r"\b(sk-ant-[A-Za-z0-9_-]+|sk-[A-Za-z0-9_-]+)"),
        REDACTION,
    ),
    (
        re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.DOTALL),
        REDACTION,
    ),
)


def redact_sensitive_text(text: str) -> str:
    redacted = str(text)
    for pattern, replacement in TEXT_PATTERNS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def is_sensitive_key(key: Any) -> bool:
    normalized = str(key or "")
    return bool(SENSITIVE_KEY_RE.search(normalized))


def sanitize(value: Any) -> Any:
    if isinstance(value, str):
        return redact_sensitive_text(value)
    if isinstance(value, dict):
        return {
            copy.deepcopy(key): REDACTION if is_sensitive_key(key) else sanitize(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if isinstance(value, tuple):
        return tuple(sanitize(item) for item in value)
    if isinstance(value, set):
        return {sanitize(item) for item in value}
    return copy.deepcopy(value)
