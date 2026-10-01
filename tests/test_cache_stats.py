import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


import cache_stats  # noqa: E402
from cache_stats import (  # noqa: E402
    CACHE_WARMING_MINIMUM_EXPECTED_SAVINGS,
    CacheStats,
    NOISE_FLOOR_TOKENS,
    format_trace,
    get_cache_warming_delay_ms,
    is_warming_worthwhile,
    record_usage,
    session_stats,
    should_warm,
    summary,
)


class _Session:
    def __init__(self, name):
        self.name = name


class CacheStatsTest(unittest.TestCase):
    def setUp(self):
        cache_stats.reset()

    def test_responses_cached_tokens_are_counted(self):
        sess = _Session("luna")
        stats = record_usage(
            {"input_tokens": 8000, "output_tokens": 50, "input_tokens_details": {"cached_tokens": 6000}},
            "responses",
            sess,
        )
        self.assertEqual(stats.input_tokens, 8000)
        self.assertEqual(stats.cached_tokens, 6000)
        self.assertEqual(stats.hit_rate, 0.75)
        self.assertEqual(stats.missed_tokens, 2000)
        self.assertEqual(session_stats(sess)["cached_tokens"], 6000)

    def test_chat_completions_details_are_counted(self):
        stats = record_usage(
            {"prompt_tokens": 2000, "completion_tokens": 10, "prompt_tokens_details": {"cached_tokens": 1500}},
            "chat_completions",
            _Session("flash"),
        )
        self.assertEqual(stats.cached_tokens, 1500)
        self.assertEqual(stats.hit_rate, 0.75)

    def test_messages_hit_rate_uses_full_prompt(self):
        # Anthropic excludes cache reads/writes from input_tokens, so the full
        # prompt is input + creation + read.
        stats = record_usage(
            {
                "input_tokens": 100,
                "output_tokens": 20,
                "cache_creation_input_tokens": 400,
                "cache_read_input_tokens": 3500,
            },
            "messages",
            _Session("claude"),
        )
        self.assertEqual(stats.hit_rate, 0.875)
        # Cache writes were still processed from scratch. 100 + 400 = 500 is
        # below the noise floor, so this reports 0 until the miss is material.
        self.assertEqual(stats.missed_tokens, 0)

    def test_missed_tokens_ignores_noise_below_the_floor(self):
        stats = CacheStats(api_mode="chat_completions", requests=1, input_tokens=NOISE_FLOOR_TOKENS, cached_tokens=0)
        self.assertEqual(stats.missed_tokens, 0)

    def test_totals_accumulate_across_requests(self):
        sess = _Session("luna")
        record_usage({"input_tokens": 1000, "output_tokens": 5,
                      "input_tokens_details": {"cached_tokens": 500}}, "responses", sess)
        record_usage({"input_tokens": 1000, "output_tokens": 5,
                      "input_tokens_details": {"cached_tokens": 900}}, "responses", sess)
        stats = session_stats(sess)
        self.assertEqual(stats["requests"], 2)
        self.assertEqual(stats["cached_tokens"], 1400)
        self.assertEqual(stats["last_cached_tokens"], 900)

    def test_summary_aggregates_sessions(self):
        record_usage({"input_tokens": 100, "output_tokens": 1,
                      "input_tokens_details": {"cached_tokens": 50}}, "responses", _Session("a"))
        record_usage({"input_tokens": 100, "output_tokens": 1,
                      "input_tokens_details": {"cached_tokens": 50}}, "responses", _Session("b"))
        totals = summary()["totals"]
        self.assertEqual(totals["requests"], 2)
        self.assertEqual(totals["input_tokens"], 200)
        self.assertEqual(totals["hit_rate"], 0.5)
        self.assertEqual(totals["cached_tokens_total"], 100)

    def test_empty_usage_is_ignored(self):
        self.assertIsNone(record_usage({}, "responses", _Session("x")))
        self.assertIsNone(record_usage(None, "responses", _Session("x")))
        self.assertEqual(summary()["totals"]["requests"], 0)

    def test_unknown_api_mode_returns_none(self):
        self.assertIsNone(record_usage({"input_tokens": 5}, "made_up", None))


class CacheTraceTest(unittest.TestCase):
    """The printed trace must stay byte-compatible with the old output."""

    def test_responses_trace(self):
        self.assertEqual(
            format_trace({"input_tokens": 10, "input_tokens_details": {"cached_tokens": 4}}, "responses"),
            "[Cache] input=10 cached=4",
        )

    def test_chat_completions_trace(self):
        self.assertEqual(
            format_trace({"prompt_tokens": 10, "prompt_tokens_details": {"cached_tokens": 7}}, "chat_completions"),
            "[Cache] input=10 cached=7",
        )

    def test_messages_trace(self):
        self.assertEqual(
            format_trace({"input_tokens": 10, "cache_creation_input_tokens": 2,
                          "cache_read_input_tokens": 3}, "messages"),
            "[Cache] input=10 creation=2 read=3",
        )


class CacheWarmingTest(unittest.TestCase):
    """Warming policy mirrors Pi's cache-warmer.ts."""

    def test_delay_is_ninety_percent_of_ttl(self):
        self.assertEqual(get_cache_warming_delay_ms(300_000), 270_000)

    def test_delay_keeps_a_ten_second_margin_on_short_ttls(self):
        # min(90% of TTL, TTL - 10s) so a short TTL still keeps its margin.
        self.assertEqual(get_cache_warming_delay_ms(15_000), 5_000)

    def test_ttl_too_short_is_not_worth_warming(self):
        self.assertIsNone(get_cache_warming_delay_ms(10_000))
        self.assertIsNone(get_cache_warming_delay_ms(5_000))

    def test_warming_only_when_savings_clear_the_floor(self):
        self.assertTrue(is_warming_worthwhile(CACHE_WARMING_MINIMUM_EXPECTED_SAVINGS))
        self.assertFalse(is_warming_worthwhile(CACHE_WARMING_MINIMUM_EXPECTED_SAVINGS - 0.01))

    def test_should_warm_requires_aged_ttl_and_savings(self):
        ttl = 300_000
        self.assertFalse(should_warm(age_ms=1000, ttl_ms=ttl, expected_savings=1.0))
        self.assertTrue(should_warm(age_ms=280_000, ttl_ms=ttl, expected_savings=1.0))
        self.assertFalse(should_warm(age_ms=280_000, ttl_ms=ttl, expected_savings=0.001))

    def test_should_warm_rejects_ancient_entries(self):
        self.assertFalse(should_warm(age_ms=cache_stats.MAX_WARMING_AGE_MS + 1,
                                     ttl_ms=300_000, expected_savings=1.0))


if __name__ == "__main__":
    unittest.main()
