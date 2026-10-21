import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from genericagent.tools.web_fetch.egress import (  # noqa: E402
    EgressConfig,
    build_egress_config,
    build_session,
)
from genericagent.tools.web_fetch.models import FetchError  # noqa: E402


class WebFetchEgressTest(unittest.TestCase):
    def test_session_uses_process_environment_proxy_settings(self):
        session = build_session(EgressConfig())
        try:
            self.assertTrue(session.trust_env)
            self.assertEqual("transparent", EgressConfig().mode)
        finally:
            session.close()

    def test_config_reads_proxy_without_exposing_it_in_fetch_arguments(self):
        env = {
            "HTTPS_PROXY": "http://127.0.0.1:7897",
            "NO_PROXY": "localhost,127.0.0.1",
        }
        config = build_egress_config(env)

        self.assertEqual("http://127.0.0.1:7897", config.proxy_url)
        self.assertEqual("localhost,127.0.0.1", config.no_proxy)
        self.assertEqual("transparent", config.mode)

    def test_socks_proxy_is_rejected_instead_of_silently_direct_connecting(self):
        for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"):
            with self.subTest(name=name):
                with self.assertRaises(FetchError) as raised:
                    build_egress_config({name: "socks5://127.0.0.1:1080"})
                self.assertEqual("EGRESS_UNAVAILABLE", raised.exception.code)

    def test_socks_in_fallback_variable_is_also_rejected(self):
        with self.assertRaises(FetchError) as raised:
            build_egress_config({
                "HTTPS_PROXY": "http://127.0.0.1:7897",
                "HTTP_PROXY": "socks5://127.0.0.1:1080",
            })
        self.assertEqual("EGRESS_UNAVAILABLE", raised.exception.code)

    def test_empty_proxy_environment_is_valid(self):
        config = build_egress_config({})
        self.assertEqual("", config.proxy_url)
        self.assertEqual("", config.no_proxy)

    def test_windows_system_proxy_is_left_to_requests_urllib_stack(self):
        # trust_env is the contract: requests delegates platform proxy discovery
        # to urllib. Avoid patching an internal implementation detail.
        with patch.dict(os.environ, {"HTTPS_PROXY": "http://127.0.0.1:7897"}):
            session = build_session(build_egress_config())
            try:
                self.assertTrue(session.trust_env)
            finally:
                session.close()


if __name__ == "__main__":
    unittest.main()
