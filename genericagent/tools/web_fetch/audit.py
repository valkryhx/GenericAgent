"""Credential-safe WebFetch audit records."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit


_SENSITIVE_QUERY_KEYS = frozenset({
    "access_token", "api_key", "apikey", "auth", "code", "credential",
    "key", "password", "secret", "session", "signature", "sig", "token",
})


def redact_url(url: str) -> str:
    try:
        parts = urlsplit(str(url or ""))
    except ValueError:
        return "<invalid-url>"
    if not parts.scheme or not parts.netloc:
        return "<invalid-url>"
    query = "redacted=1" if parts.query else ""
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, ""))


def build_audit_record(
    *,
    url: str,
    final_url: str,
    status: str,
    egress: dict,
    http_status: int | None = None,
    encoded_bytes: int = 0,
    decoded_bytes: int = 0,
    duration_ms: int = 0,
    body: str | bytes | None = None,
    headers: dict | None = None,
    cache_status: str | None = None,
    redirects=None,
    error_code: str | None = None,
) -> dict:
    return {
        "type": "web_fetch",
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "url": redact_url(url),
        "final_url": redact_url(final_url or url),
        "redirects": [redact_url(item) for item in (redirects or [])],
        "egress_mode": str((egress or {}).get("mode") or "transparent"),
        "security_assurance": str((egress or {}).get("security_assurance") or "basic"),
        "status": str(status or "unknown"),
        "http_status": http_status,
        "encoded_bytes": int(encoded_bytes or 0),
        "decoded_bytes": int(decoded_bytes or 0),
        "duration_ms": int(duration_ms or 0),
        "cache": cache_status or "miss",
        "error_code": error_code,
    }


def write_audit_record(record: dict, path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    with target.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
