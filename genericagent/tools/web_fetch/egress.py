"""Transparent process-network egress for WebFetch."""

from __future__ import annotations

from dataclasses import dataclass
import os

import requests

from .models import FetchError


_SOCKS_PREFIXES = ("socks://", "socks4://", "socks5://", "socks5h://")


@dataclass(frozen=True)
class EgressConfig:
    mode: str = "transparent"
    proxy_url: str = ""
    no_proxy: str = ""
    connect_timeout_seconds: float = 3.0
    read_timeout_seconds: float = 5.0


def _proxy_value(env) -> str:
    for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
        value = str(env.get(name) or "").strip()
        if value:
            return value
    return ""


def build_egress_config(env=None) -> EgressConfig:
    values = os.environ if env is None else env
    proxy_url = _proxy_value(values)
    proxy_values = [
        str(values.get(name) or "").strip()
        for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy")
    ]
    for value in proxy_values:
        if value.lower().startswith(_SOCKS_PREFIXES):
            raise FetchError(
                "EGRESS_UNAVAILABLE",
                "SOCKS proxies are not supported; use an HTTP/HTTPS mixed port",
                details={"proxy_scheme": value.split(":", 1)[0].lower()},
            )
    no_proxy = str(values.get("NO_PROXY") or values.get("no_proxy") or "").strip()
    return EgressConfig(proxy_url=proxy_url, no_proxy=no_proxy)


def build_session(_config: EgressConfig | None = None) -> requests.Session:
    """Create a session that inherits proxies from the process environment.

    requests resolves explicit environment proxies first and falls back to the
    platform proxy discovery implemented by urllib. TUN/VPN is below this layer.
    """

    session = requests.Session()
    session.trust_env = True
    return session
