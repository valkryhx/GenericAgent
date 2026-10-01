"""Prompt cache accounting.

Pi treats its prompt cache as a first-class asset (cache-warmer.ts,
cache-stats.ts): it measures how much of each request was served from
cache and uses that to decide when a refresh is worth sending. GA already
stamps cache_control and sends prompt_cache_key, but it only printed a
one-line trace, so a regression that silently doubled cost or latency had
nothing to trip over.

This module keeps those numbers in one place: per-session totals, a
process-wide summary, and a "missed tokens" figure that ignores the noise
floor the way Pi's cache-stats.ts does (NOISE_FLOOR_TOKENS = 1024).
"""

import threading
import time
from dataclasses import dataclass, field
from typing import Any, Optional

# Below this many tokens a cache miss is normal churn, not a regression.
NOISE_FLOOR_TOKENS = 1024


@dataclass
class CacheStats:
    """Cache accounting for one session."""

    api_mode: str = ""
    requests: int = 0
    input_tokens: int = 0
    cached_tokens: int = 0
    output_tokens: int = 0
    cache_creation_tokens: int = 0
    cache_read_tokens: int = 0
    last_cached_tokens: int = 0
    last_input_tokens: int = 0
    last_request_at: Optional[float] = None

    @property
    def hit_rate(self) -> Optional[float]:
        """Share of the prompt served from cache, or None before any request.

        Anthropic's ``input_tokens`` excludes cache reads and writes, so the
        full prompt there is input + creation + read. OpenAI-compatible APIs
        report ``cached_tokens`` inside the prompt count.
        """
        if not self.requests:
            return None
        if self.api_mode == "messages":
            prompt = self.input_tokens + self.cache_creation_tokens + self.cache_read_tokens
            return (self.cache_read_tokens / prompt) if prompt else None
        if self.input_tokens <= 0:
            return None
        return self.cached_tokens / self.input_tokens

    @property
    def missed_tokens(self) -> int:
        """Prompt tokens that were not served from cache, above the noise floor."""
        if not self.requests:
            return 0
        if self.api_mode == "messages":
            # Tokens written to the cache were still processed from scratch.
            missed = self.input_tokens + self.cache_creation_tokens
        else:
            missed = max(0, self.input_tokens - self.cached_tokens)
        return missed if missed > NOISE_FLOOR_TOKENS else 0

    def to_dict(self) -> dict[str, Any]:
        rate = self.hit_rate
        return {
            "api_mode": self.api_mode,
            "requests": self.requests,
            "input_tokens": self.input_tokens,
            "cached_tokens": self.cached_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
            "output_tokens": self.output_tokens,
            "hit_rate": None if rate is None else round(rate, 4),
            "missed_tokens": self.missed_tokens,
            "last_input_tokens": self.last_input_tokens,
            "last_cached_tokens": self.last_cached_tokens,
        }


_LOCK = threading.Lock()
_SESSIONS: dict[str, CacheStats] = {}


def _session_key(sess: Any) -> str:
    if sess is None:
        return "<none>"
    name = getattr(sess, "name", None) or getattr(sess, "model", None)
    if name:
        return str(name)
    return "session-%d" % id(sess)


def _int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def record_usage(usage: Optional[dict], api_mode: str, sess: Any = None) -> Optional[CacheStats]:
    """Fold one response's usage into the running totals.

    Returns the updated per-session stats, or None when usage carried no
    cache information for this API mode.
    """
    if not usage:
        return None
    cached = 0
    inp = 0
    out = 0
    creation = 0
    read = 0
    if api_mode == "responses":
        cached = _int((usage.get("input_tokens_details") or {}).get("cached_tokens"))
        inp = _int(usage.get("input_tokens"))
        out = _int(usage.get("output_tokens"))
    elif api_mode == "chat_completions":
        cached = _int((usage.get("prompt_tokens_details") or {}).get("cached_tokens"))
        inp = _int(usage.get("prompt_tokens"))
        out = _int(usage.get("completion_tokens"))
    elif api_mode == "messages":
        creation = _int(usage.get("cache_creation_input_tokens"))
        read = _int(usage.get("cache_read_input_tokens"))
        inp = _int(usage.get("input_tokens"))
        out = _int(usage.get("output_tokens"))
    else:
        return None
    if inp == 0 and out == 0 and cached == 0 and creation == 0 and read == 0:
        return None
    key = _session_key(sess)
    with _LOCK:
        stats = _SESSIONS.get(key)
        if stats is None:
            stats = CacheStats(api_mode=api_mode)
            _SESSIONS[key] = stats
        stats.api_mode = api_mode
        stats.requests += 1
        stats.input_tokens += inp
        stats.cached_tokens += cached
        stats.output_tokens += out
        stats.cache_creation_tokens += creation
        stats.cache_read_tokens += read
        stats.last_input_tokens = inp
        stats.last_cached_tokens = read if api_mode == "messages" else cached
        stats.last_request_at = time.time()
        return stats


def session_stats(sess: Any = None) -> Optional[dict[str, Any]]:
    with _LOCK:
        stats = _SESSIONS.get(_session_key(sess))
        return None if stats is None else stats.to_dict()


def summary() -> dict[str, Any]:
    """Process-wide totals, for diagnostics and tests."""
    with _LOCK:
        sessions = {name: stats.to_dict() for name, stats in _SESSIONS.items()}
    totals = {
        "requests": sum(s["requests"] for s in sessions.values()),
        "input_tokens": sum(s["input_tokens"] for s in sessions.values()),
        "cached_tokens": sum(s["cached_tokens"] for s in sessions.values()),
        "cache_read_tokens": sum(s["cache_read_tokens"] for s in sessions.values()),
        "output_tokens": sum(s["output_tokens"] for s in sessions.values()),
    }
    # OpenAI-compatible APIs report cached_tokens inside the prompt count;
    # Anthropic reports cache reads separately in cache_read_tokens. Both feed
    # the same hit-rate numerator so the totals stay meaningful across modes.
    cached_total = totals["cached_tokens"] + totals["cache_read_tokens"]
    prompt_total = totals["input_tokens"] + totals["cache_read_tokens"]
    totals["cached_tokens_total"] = cached_total
    totals["hit_rate"] = round(cached_total / prompt_total, 4) if prompt_total else None
    return {"sessions": sessions, "totals": totals}


def reset() -> None:
    with _LOCK:
        _SESSIONS.clear()


def format_trace(usage: Optional[dict], api_mode: str) -> str:
    """The one-line ``[Cache]`` trace, kept compatible with the old output."""
    if not usage:
        return ""
    if api_mode == "responses":
        return "[Cache] input=%s cached=%s" % (
            _int(usage.get("input_tokens")),
            _int((usage.get("input_tokens_details") or {}).get("cached_tokens")),
        )
    if api_mode == "chat_completions":
        return "[Cache] input=%s cached=%s" % (
            _int(usage.get("prompt_tokens")),
            _int((usage.get("prompt_tokens_details") or {}).get("cached_tokens")),
        )
    if api_mode == "messages":
        return "[Cache] input=%s creation=%s read=%s" % (
            _int(usage.get("input_tokens")),
            _int(usage.get("cache_creation_input_tokens")),
            _int(usage.get("cache_read_input_tokens")),
        )
    return ""


# --- Cache warming -------------------------------------------------------
#
# Mirrors Pi's cache-warmer.ts: refresh at 90% of the entry's TTL with a
# margin, and only spend a request when the expected saving clears a floor.

# Never continue warming this long after the request that started it.
MAX_WARMING_AGE_MS = 60 * 60 * 1000
# Sent only when the refresh is expected to save at least this much (USD).
CACHE_WARMING_MINIMUM_EXPECTED_SAVINGS = 0.05


def get_cache_warming_delay_ms(ttl_ms: int) -> Optional[int]:
    """When to refresh, in ms. None when the TTL is too short to bother."""
    if ttl_ms <= 10_000:
        return None
    return max(1, min(int(ttl_ms * 0.9), ttl_ms - 10_000))


def is_warming_worthwhile(
    expected_savings: float,
    minimum_savings: float = CACHE_WARMING_MINIMUM_EXPECTED_SAVINGS,
) -> bool:
    """Whether a warm request is worth its own cost."""
    try:
        return float(expected_savings) >= float(minimum_savings)
    except (TypeError, ValueError):
        return False


def should_warm(
    age_ms: float,
    ttl_ms: int,
    expected_savings: float,
    minimum_savings: float = CACHE_WARMING_MINIMUM_EXPECTED_SAVINGS,
) -> bool:
    """Whether an idle cache entry should be refreshed now."""
    delay = get_cache_warming_delay_ms(ttl_ms)
    if delay is None or age_ms < 0 or age_ms > MAX_WARMING_AGE_MS:
        return False
    if age_ms < delay:
        return False
    return is_warming_worthwhile(expected_savings, minimum_savings)
