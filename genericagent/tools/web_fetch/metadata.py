"""Narrow cloud-metadata address guard for A-route WebFetch."""

from __future__ import annotations

import os
import socket

from .models import FetchError


METADATA_IPS = frozenset({
    "169.254.169.254",
    "169.254.170.2",
    "100.100.100.200",
    "168.63.129.16",
    "192.0.0.192",
})


def metadata_guard_enabled(env=None) -> bool:
    values = os.environ if env is None else env
    raw = str(values.get("GA_WEBFETCH_BLOCK_METADATA_IPS", "1")).strip().lower()
    return raw not in {"0", "false", "no", "off"}


def check_metadata_addresses(
    hostname: str,
    *,
    enabled: bool = True,
    resolver=socket.getaddrinfo,
) -> tuple[str, ...]:
    """Resolve once and reject only cloud-metadata addresses.

    This is intentionally not a general SSRF check. The subsequent connection
    resolves again, so this must never be described as DNS-rebinding safe.
    """

    if not enabled:
        return ()
    try:
        infos = resolver(hostname, None, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError) as exc:
        raise FetchError(
            "EGRESS_UNAVAILABLE",
            f"Could not resolve hostname: {hostname}",
            details={"host": hostname},
        ) from exc
    addresses = tuple(
        dict.fromkeys(
            str(info[4][0])
            for info in infos
            if len(info) >= 5 and info[4]
        )
    )
    blocked = tuple(address for address in addresses if address in METADATA_IPS)
    if blocked:
        raise FetchError(
            "METADATA_ADDRESS_BLOCKED",
            "Hostname resolves to a cloud metadata address",
            details={"host": hostname, "addresses": list(blocked)},
        )
    return addresses
