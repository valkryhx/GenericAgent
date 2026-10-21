"""Small bounded on-disk cache for successful WebFetch results."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import time


POLICY_VERSION = "ga-web-fetch-a-v2"


def build_cache_key(
    *,
    url: str,
    offset: int,
    egress_mode: str,
    security_assurance: str,
    policy_version: str = POLICY_VERSION,
) -> str:
    material = json.dumps(
        {
            "url": str(url),
            "offset": int(offset),
            "egress_mode": str(egress_mode),
            "security_assurance": str(security_assurance),
            "policy_version": str(policy_version),
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class WebFetchCache:
    def __init__(self, root, *, ttl_seconds: int = 300, max_entries: int = 128):
        self.root = Path(root)
        self.ttl_seconds = max(0, int(ttl_seconds))
        self.max_entries = max(1, int(max_entries))

    def _path(self, key: str) -> Path:
        return self.root / f"{key}.json"

    def get(self, key: str) -> dict | None:
        path = self._path(key)
        try:
            stat = path.stat()
            if time.time() - stat.st_mtime > self.ttl_seconds:
                return None
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None
        return payload if isinstance(payload, dict) else None

    def put(self, key: str, payload: dict) -> bool:
        if not isinstance(payload, dict) or payload.get("status") != "success":
            return False
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            path = self._path(key)
            temp = path.with_suffix(".tmp")
            temp.write_text(
                json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
                encoding="utf-8",
                newline="\n",
            )
            os.replace(temp, path)
            self._trim()
            return True
        except OSError:
            return False

    def _trim(self) -> None:
        try:
            files = sorted(
                (item for item in self.root.glob("*.json") if item.is_file()),
                key=lambda item: item.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return
        for stale in files[self.max_entries:]:
            try:
                stale.unlink()
            except OSError:
                pass
