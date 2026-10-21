"""URL and hostname policy for transparent WebFetch."""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import ip_address
import re
from urllib.parse import urlsplit, urlunsplit

from .models import FetchError


MAX_URL_LENGTH = 2000
_BLOCKED_HOSTS = frozenset({
    "0.0.0.0",
    "[::]",
    "localhost",
    "metadata",
    "metadata.google.internal",
    "metadata.google.internal.",
    "instance-data",
    "instance-data.ec2.internal",
    "metadata.azure.internal",
    "metadata.tencentyun.com",
    "metadata.tencent.internal",
})
_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f]")
_DEFAULT_PORTS = {"http": 80, "https": 443}
_EXPLICIT_PORTS = frozenset({80, 443, 8080, 8443})


@dataclass(frozen=True)
class ValidatedUrl:
    url: str
    scheme: str
    hostname: str
    port: int | None


def _normalized_host(hostname: str) -> str:
    return hostname.rstrip(".").lower()


def _is_ip_literal(host: str) -> bool:
    candidate = host.strip("[]")
    try:
        ip_address(candidate)
        return True
    except ValueError:
        return False


def _blocked_host(host: str) -> bool:
    host = _normalized_host(host)
    if host in _BLOCKED_HOSTS or host.endswith(".localhost"):
        return True
    if "169.254.169.254" in host or "metadata.google.internal" in host:
        return True
    return False


def validate_url(url: object, *, allow_http: bool = False) -> ValidatedUrl:
    """Validate a URL without resolving its hostname.

    This is deliberately A-route policy: it blocks static local/metadata forms
    but does not claim general private-address or DNS-rebinding protection.
    """

    text = str(url or "").strip()
    if not text:
        raise FetchError("URL_INVALID", "URL is required")
    if len(text) > MAX_URL_LENGTH:
        raise FetchError(
            "URL_TOO_LONG",
            f"URL exceeds {MAX_URL_LENGTH} characters",
            details={"length": len(text), "limit": MAX_URL_LENGTH},
        )
    if _CONTROL_CHARS.search(text):
        raise FetchError("URL_INVALID", "URL contains control characters")

    try:
        parts = urlsplit(text)
        if "\\" in parts.netloc:
            raise ValueError("backslashes are not allowed in URL authority")
        hostname = parts.hostname
        port = parts.port
    except ValueError as exc:
        raise FetchError("URL_INVALID", f"Invalid URL: {exc}") from exc

    scheme = str(parts.scheme or "").lower()
    if not scheme:
        raise FetchError("URL_INVALID", "URL must be absolute and include a scheme")
    allowed_schemes = {"https"} | ({"http"} if allow_http else set())
    if scheme not in allowed_schemes:
        raise FetchError(
            "SCHEME_BLOCKED",
            "Only HTTPS URLs are allowed",
            details={"scheme": scheme},
        )
    if not parts.netloc or not hostname:
        raise FetchError("URL_INVALID", "URL must include a valid host")

    host = _normalized_host(hostname)
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise FetchError("URL_USERINFO", "URL userinfo is not allowed")
    if _is_ip_literal(host):
        raise FetchError("HOST_BLOCKED", "IP literal hosts are not allowed", details={"host": host})
    if _blocked_host(host):
        raise FetchError("HOST_BLOCKED", "Host is not allowed", details={"host": host})
    if "." not in host:
        raise FetchError("HOST_DOTLESS", "Dotless hostnames are not allowed", details={"host": host})
    if port is not None and port not in _EXPLICIT_PORTS:
        raise FetchError(
            "HOST_BLOCKED",
            "Non-standard target ports are not allowed",
            details={"host": host, "port": port},
        )

    normalized_host = host
    if ":" in normalized_host and not normalized_host.startswith("["):
        normalized_host = f"[{normalized_host}]"
    default_port = _DEFAULT_PORTS.get(scheme)
    netloc = normalized_host if port in (None, default_port) else f"{normalized_host}:{port}"
    normalized = urlunsplit((scheme, netloc, parts.path or "/", parts.query, ""))
    return ValidatedUrl(normalized, scheme, host, port)
