"""Request and error contracts for WebFetch."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class WebFetchRequest:
    url: str
    prompt: str = ""
    offset: int = 0


class FetchError(Exception):
    """A stable, typed WebFetch failure."""

    def __init__(self, code: str, message: str, *, details: dict | None = None):
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)
        self.details = dict(details or {})

    def to_dict(self) -> dict:
        return {
            "status": "error",
            "error": {
                "code": self.code,
                "message": self.message,
                "details": dict(self.details),
            },
        }
