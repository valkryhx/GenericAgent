import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from genericagent.tools.web_fetch.cache import WebFetchCache, build_cache_key  # noqa: E402


class WebFetchCacheTest(unittest.TestCase):
    def test_key_separates_egress_security_and_policy_version(self):
        base = dict(url="https://example.com/", offset=0)
        a = build_cache_key(**base, egress_mode="transparent", security_assurance="basic", policy_version="v1")
        b = build_cache_key(**base, egress_mode="trusted_proxy", security_assurance="basic", policy_version="v1")
        c = build_cache_key(**base, egress_mode="transparent", security_assurance="verified", policy_version="v1")
        d = build_cache_key(**base, egress_mode="transparent", security_assurance="basic", policy_version="v2")
        self.assertEqual(4, len({a, b, c, d}))

    def test_round_trip_and_expiry(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = WebFetchCache(Path(tmp) / "cache", ttl_seconds=60, max_entries=2)
            key = build_cache_key(
                url="https://example.com/",
                offset=0,
                egress_mode="transparent",
                security_assurance="basic",
                policy_version="v1",
            )
            cache.put(key, {"status": "success", "result": "cached"})
            self.assertEqual("cached", cache.get(key)["result"])

    def test_disabled_or_unsuccessful_results_are_not_written(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = WebFetchCache(Path(tmp) / "cache", ttl_seconds=60, max_entries=2)
            key = build_cache_key(
                url="https://example.com/",
                offset=0,
                egress_mode="transparent",
                security_assurance="basic",
                policy_version="v1",
            )
            cache.put(key, {"status": "error", "error": {"code": "DEADLINE"}})
            self.assertIsNone(cache.get(key))


if __name__ == "__main__":
    unittest.main()
