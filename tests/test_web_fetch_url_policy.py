import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from genericagent.tools.web_fetch.policy import validate_url


class WebFetchUrlPolicyTest(unittest.TestCase):
    def assert_code(self, url, code):
        with self.assertRaises(Exception) as raised:
            validate_url(url)
        self.assertEqual(code, getattr(raised.exception, "code", None))

    def test_https_url_is_accepted_and_normalized(self):
        result = validate_url("HTTPS://Example.COM/path")

        self.assertEqual("https", result.scheme)
        self.assertEqual("example.com", result.hostname)
        self.assertEqual("https://example.com/path", result.url)
        self.assertEqual("https://example.com/path", validate_url("https://example.com/path#section").url)

    def test_http_is_rejected_by_default(self):
        self.assert_code("http://example.com/", "SCHEME_BLOCKED")

    def test_http_can_be_enabled_for_private_legacy_callers(self):
        result = validate_url("http://example.com/", allow_http=True)
        self.assertEqual("http", result.scheme)

    def test_userinfo_is_rejected(self):
        self.assert_code("https://user:pass@example.com/", "URL_USERINFO")

    def test_control_characters_are_rejected(self):
        self.assert_code("https://example.com/a\nb", "URL_INVALID")

    def test_backslash_authority_confusion_is_rejected(self):
        self.assert_code(r"https:\example.com\@evil.example/", "URL_INVALID")

    def test_dotless_host_is_rejected(self):
        self.assert_code("https://intranet/", "HOST_DOTLESS")

    def test_ipv4_literal_is_rejected(self):
        self.assert_code("https://127.0.0.1/", "HOST_BLOCKED")

    def test_ipv6_literal_is_rejected(self):
        self.assert_code("https://[::1]/", "HOST_BLOCKED")
        self.assert_code("https://0.0.0.0/", "HOST_BLOCKED")
        self.assert_code("https://[::]/", "HOST_BLOCKED")

    def test_localhost_is_rejected(self):
        self.assert_code("https://localhost/", "HOST_BLOCKED")
        self.assert_code("https://api.localhost/", "HOST_BLOCKED")

    def test_metadata_hostname_is_rejected(self):
        self.assert_code("https://metadata.google.internal/", "HOST_BLOCKED")
        self.assert_code("https://169.254.169.254.nip.io/", "HOST_BLOCKED")
        self.assert_code("https://metadata.azure.internal/", "HOST_BLOCKED")
        self.assert_code("https://metadata.tencentyun.com/", "HOST_BLOCKED")

    def test_non_standard_port_is_rejected(self):
        self.assert_code("https://example.com:6379/", "HOST_BLOCKED")

    def test_standard_alt_port_is_allowed(self):
        self.assertEqual(8443, validate_url("https://example.com:8443/").port)

    def test_url_longer_than_limit_is_rejected(self):
        self.assert_code("https://example.com/" + "a" * 2000, "URL_TOO_LONG")

    def test_empty_and_relative_urls_are_rejected(self):
        self.assert_code("", "URL_INVALID")
        self.assert_code("/relative", "URL_INVALID")


if __name__ == "__main__":
    unittest.main()
