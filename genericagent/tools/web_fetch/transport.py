"""Bounded HTTP transport with manual redirect handling."""

from __future__ import annotations

from dataclasses import dataclass, field
import gzip
import io
import os
import time
import zlib
from urllib.parse import urljoin

import requests

from .metadata import check_metadata_addresses, metadata_guard_enabled
from .models import FetchError
from .policy import validate_url


@dataclass(frozen=True)
class FetchLimits:
    max_redirects: int = 5
    max_header_bytes: int = 16 * 1024
    max_encoded_bytes: int = 2 * 1024 * 1024
    max_decoded_bytes: int = 10 * 1024 * 1024
    deadline_seconds: float = 15.0
    connect_timeout_seconds: float = 3.0
    read_timeout_seconds: float = 5.0
    chunk_size: int = 64 * 1024


@dataclass
class FetchResponse:
    requested_url: str
    final_url: str
    status_code: int
    headers: dict
    body: bytes
    redirects: list[str] = field(default_factory=list)
    truncated: bool = False
    encoded_bytes: int = 0
    decoded_bytes: int = 0


_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})
_DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)
_DEFAULT_HEADERS = {
    "Accept": "text/markdown, text/html, text/plain, application/json, application/xml, */*",
    "Accept-Encoding": "gzip",
}


def user_agent(env=None) -> str:
    values = os.environ if env is None else env
    configured = str(values.get("GA_WEBFETCH_USER_AGENT") or "").strip()
    return configured or _DEFAULT_USER_AGENT


def request_headers(env=None) -> dict[str, str]:
    headers = dict(_DEFAULT_HEADERS)
    agent = user_agent(env)
    if any(ord(char) < 32 or ord(char) == 127 for char in agent):
        raise FetchError("URL_INVALID", "User-Agent contains control characters")
    headers["User-Agent"] = agent[:512]
    return headers


def _cancel_requested(stop_signal) -> bool:
    if stop_signal is None:
        return False
    is_set = getattr(stop_signal, "is_set", None)
    return bool(is_set()) if callable(is_set) else bool(stop_signal)


def _deadline_expired(started: float, deadline_seconds: float) -> bool:
    return time.monotonic() - started >= deadline_seconds


def _header_size(headers) -> int:
    total = 0
    for key, value in (headers or {}).items():
        total += len(str(key).encode("utf-8", "replace"))
        total += len(str(value).encode("utf-8", "replace"))
        total += 4
    return total


def _read_encoded(response, limits: FetchLimits, started: float, deadline_seconds: float, stop_signal=None) -> bytes:
    content_length = str(
        (response.headers or {}).get("Content-Length")
        or (response.headers or {}).get("content-length")
        or ""
    ).strip()
    if content_length.isdigit() and int(content_length) > limits.max_encoded_bytes:
        raise FetchError(
            "BODY_TOO_LARGE",
            f"Encoded response exceeds {limits.max_encoded_bytes} bytes",
            details={"content_length": int(content_length), "limit": limits.max_encoded_bytes},
        )

    output = bytearray()
    for chunk in response.iter_content(chunk_size=limits.chunk_size):
        if _cancel_requested(stop_signal):
            raise FetchError("CANCELLED", "WebFetch was cancelled")
        if _deadline_expired(started, deadline_seconds):
            raise FetchError("DEADLINE", "WebFetch deadline exceeded")
        if not chunk:
            continue
        output.extend(chunk)
        if len(output) > limits.max_encoded_bytes:
            raise FetchError(
                "BODY_TOO_LARGE",
                f"Encoded response exceeds {limits.max_encoded_bytes} bytes",
                details={"limit": limits.max_encoded_bytes},
            )
    return bytes(output)


def _looks_like_gzip(data: bytes) -> bool:
    return len(data) >= 2 and data[:2] == bytes((0x1F, 0x8B))


def _looks_like_deflate(data: bytes) -> bool:
    if len(data) < 2:
        return False
    first = data[0]
    return first & 0x0F == 8 and (first << 8 | data[1]) % 31 == 0


def _decode_body(encoded: bytes, content_encoding: str, limit: int) -> bytes:
    encoding = str(content_encoding or "").strip().lower()
    if not encoding or encoding == "identity":
        decoded = encoded
    elif encoding == "gzip":
        if _looks_like_gzip(encoded):
            try:
                with gzip.GzipFile(fileobj=io.BytesIO(encoded)) as stream:
                    decoded = stream.read(limit + 1)
            except (OSError, EOFError, zlib.error) as exc:
                raise FetchError("DECODE_FAILED", "Invalid gzip response body") from exc
        else:
            # urllib3 may transparently decode while retaining Content-Encoding.
            decoded = encoded
    elif encoding == "deflate":
        if _looks_like_deflate(encoded):
            try:
                decoded = zlib.decompress(encoded, zlib.MAX_WBITS | 32)
            except zlib.error:
                try:
                    decoded = zlib.decompress(encoded, -zlib.MAX_WBITS)
                except zlib.error as exc:
                    raise FetchError("DECODE_FAILED", "Invalid deflate response body") from exc
        else:
            decoded = encoded
    else:
        raise FetchError(
            "DECODE_FAILED",
            f"Unsupported Content-Encoding: {encoding}",
            details={"content_encoding": encoding},
        )

    if len(decoded) > limit:
        raise FetchError(
            "DECODED_BODY_TOO_LARGE",
            f"Decoded response exceeds {limit} bytes",
            details={"limit": limit},
        )
    return decoded


def fetch_once(
    session,
    url: str,
    *,
    limits: FetchLimits | None = None,
    stop_signal=None,
    deadline_seconds: float | None = None,
    allow_http: bool = False,
    check_metadata: bool = True,
    resolver=None,
) -> FetchResponse:
    limits = limits or FetchLimits()
    deadline = limits.deadline_seconds if deadline_seconds is None else deadline_seconds
    started = time.monotonic()
    validated = validate_url(url, allow_http=allow_http)
    if check_metadata and metadata_guard_enabled():
        metadata_kwargs = {"enabled": True}
        if resolver is not None:
            metadata_kwargs["resolver"] = resolver
        check_metadata_addresses(validated.hostname, **metadata_kwargs)

    if _cancel_requested(stop_signal):
        raise FetchError("CANCELLED", "WebFetch was cancelled")
    if _deadline_expired(started, deadline):
        raise FetchError("DEADLINE", "WebFetch deadline exceeded")

    response = None
    try:
        response = session.get(
            validated.url,
            stream=True,
            timeout=(limits.connect_timeout_seconds, limits.read_timeout_seconds),
            allow_redirects=False,
            headers=request_headers(),
            verify=True,
        )
        if _cancel_requested(stop_signal):
            raise FetchError("CANCELLED", "WebFetch was cancelled")
        if _deadline_expired(started, deadline):
            raise FetchError("DEADLINE", "WebFetch deadline exceeded")
        if _header_size(response.headers) > limits.max_header_bytes:
            raise FetchError(
                "HEADER_TOO_LARGE",
                f"Response headers exceed {limits.max_header_bytes} bytes",
                details={"limit": limits.max_header_bytes},
            )

        status = int(getattr(response, "status_code", 0) or 0)
        headers = {
            str(key).lower(): value
            for key, value in dict(response.headers or {}).items()
        }
        if status in _REDIRECT_STATUSES:
            location = str(headers.get("location") or headers.get("Location") or "").strip()
            if not location:
                raise FetchError("REDIRECT_INVALID", "Redirect response has no Location header")
            return FetchResponse(
                requested_url=validated.url,
                final_url=urljoin(validated.url, location),
                status_code=status,
                headers=headers,
                body=b"",
            )

        if status < 200 or status >= 300:
            raise FetchError(
                "HTTP_STATUS",
                f"Remote server returned HTTP {status}",
                details={"status": status},
            )
        encoded = _read_encoded(response, limits, started, deadline, stop_signal)
        if _cancel_requested(stop_signal):
            raise FetchError("CANCELLED", "WebFetch was cancelled")
        if _deadline_expired(started, deadline):
            raise FetchError("DEADLINE", "WebFetch deadline exceeded")
        decoded = _decode_body(
            encoded,
            headers.get("content-encoding", ""),
            limits.max_decoded_bytes,
        )
        return FetchResponse(
            requested_url=validated.url,
            final_url=str(getattr(response, "url", "") or validated.url),
            status_code=status,
            headers=headers,
            body=decoded,
            encoded_bytes=len(encoded),
            decoded_bytes=len(decoded),
        )
    except FetchError:
        raise
    except requests.Timeout as exc:
        raise FetchError("REQUEST_TIMEOUT", "WebFetch request timed out") from exc
    except requests.RequestException as exc:
        raise FetchError("EGRESS_UNAVAILABLE", f"WebFetch request failed: {exc}") from exc
    finally:
        if response is not None:
            response.close()


def _site_identity(validated) -> tuple[str, int | None]:
    return validated.hostname, validated.port


def _redirect_target(result: FetchResponse, current: str) -> str:
    headers = result.headers or {}
    location = str(headers.get("location") or headers.get("Location") or "").strip()
    if not location:
        raise FetchError("REDIRECT_INVALID", "Redirect response has no Location header")
    return urljoin(current, location)


def fetch_with_redirects(
    session,
    url: str,
    *,
    limits: FetchLimits | None = None,
    stop_signal=None,
    allow_http: bool = False,
    check_metadata: bool = True,
    resolver=None,
) -> FetchResponse:
    limits = limits or FetchLimits()
    started = time.monotonic()
    current = validate_url(url, allow_http=allow_http).url
    redirects: list[str] = []

    for _ in range(limits.max_redirects + 1):
        result = fetch_once(
            session,
            current,
            limits=limits,
            stop_signal=stop_signal,
            deadline_seconds=max(0.0, limits.deadline_seconds - (time.monotonic() - started)),
            allow_http=allow_http,
            check_metadata=check_metadata,
            resolver=resolver,
        )
        if result.status_code not in _REDIRECT_STATUSES:
            result.redirects = redirects
            return result
        if len(redirects) >= limits.max_redirects:
            raise FetchError(
                "REDIRECT_LIMIT",
                f"Redirect count exceeds {limits.max_redirects}",
                details={"limit": limits.max_redirects},
            )

        source = validate_url(current, allow_http=allow_http)
        target = validate_url(_redirect_target(result, current), allow_http=allow_http)
        if _site_identity(source) != _site_identity(target):
            raise FetchError(
                "REDIRECT_CROSS_SITE",
                "Cross-site redirects are not followed automatically",
                details={"from": source.url, "to": target.url},
            )
        redirects.append(target.url)
        current = target.url

    raise FetchError(
        "REDIRECT_LIMIT",
        f"Redirect count exceeds {limits.max_redirects}",
        details={"limit": limits.max_redirects},
    )
