import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from genericagent.tools.web_fetch import tool as web_fetch_tool  # noqa: E402
from genericagent.tools.web_fetch.transport import FetchResponse  # noqa: E402


class WebFetchToolTest(unittest.TestCase):
    def test_success_has_transparent_basic_contract_and_untrusted_boundary(self):
        response = FetchResponse(
            requested_url="https://example.com/",
            final_url="https://example.com/",
            status_code=200,
            headers={"content-type": "text/html; charset=utf-8"},
            body=b"<h1>Hello</h1>",
            encoded_bytes=14,
            decoded_bytes=14,
        )
        with patch.object(web_fetch_tool, "fetch_with_redirects", return_value=response):
            result = web_fetch_tool.run_web_fetch(url="https://example.com/")

        self.assertEqual("success", result["status"])
        self.assertIn("# Hello", result["result"])
        self.assertIn("<fetched-web-content>", result["result"])
        self.assertEqual("transparent", result["egress"]["mode"])
        self.assertEqual("basic", result["egress"]["security_assurance"])

    def test_enabled_cache_returns_cached_payload_without_second_fetch(self):
        response = FetchResponse(
            requested_url="https://example.com/",
            final_url="https://example.com/",
            status_code=200,
            headers={"content-type": "text/plain"},
            body=b"cached body",
            encoded_bytes=11,
            decoded_bytes=11,
        )
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"GA_WEBFETCH_CACHE_DIR": tmp}, clear=False
        ), patch.object(
            web_fetch_tool, "fetch_with_redirects", return_value=response
        ) as fetch:
            first = web_fetch_tool.run_web_fetch(url="https://example.com/")
            second = web_fetch_tool.run_web_fetch(url="https://example.com/")

        self.assertEqual("success", first["status"])
        self.assertEqual(first, second)
        self.assertEqual(1, fetch.call_count)

    def test_audit_record_omits_body_and_redacts_query(self):
        response = FetchResponse(
            requested_url="https://example.com/",
            final_url="https://example.com/",
            status_code=200,
            headers={"content-type": "text/plain"},
            body=b"SECRET PAGE BODY",
            encoded_bytes=17,
            decoded_bytes=17,
        )
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "audit.jsonl"
            with patch.dict(
                os.environ, {"GA_WEBFETCH_AUDIT_PATH": str(audit_path)}, clear=False
            ), patch.object(web_fetch_tool, "fetch_with_redirects", return_value=response):
                result = web_fetch_tool.run_web_fetch(
                    url="https://example.com/?token=secret"
                )

            self.assertEqual("success", result["status"])
            audit = audit_path.read_text(encoding="utf-8")
            self.assertNotIn("SECRET PAGE BODY", audit)
            self.assertNotIn("token=secret", audit)
            self.assertIn('"egress_mode": "transparent"', audit)
            self.assertIn('"security_assurance": "basic"', audit)

    def test_typed_error_is_returned_without_leaking_traceback(self):
        calls = []

        def fail(*args, **kwargs):
            calls.append((args, kwargs))
            raise web_fetch_tool.FetchError("SCHEME_BLOCKED", "HTTPS only")

        with patch.object(web_fetch_tool, "fetch_with_redirects", side_effect=fail):
            result = web_fetch_tool.run_web_fetch(url="http://example.com/")

        self.assertEqual("error", result["status"])
        self.assertEqual("SCHEME_BLOCKED", result["error"]["code"])
        self.assertEqual(1, len(calls))

    def test_empty_html_returns_empty_content_and_is_not_cached(self):
        response = FetchResponse(
            requested_url="https://example.com/",
            final_url="https://example.com/",
            status_code=200,
            headers={"content-type": "text/html; charset=utf-8"},
            body=b"<html><body><script>location.replace(location.href)</script></body></html>",
            encoded_bytes=70,
            decoded_bytes=70,
        )
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ, {"GA_WEBFETCH_CACHE_DIR": tmp}, clear=False
        ), patch.object(web_fetch_tool, "fetch_with_redirects", return_value=response):
            first = web_fetch_tool.run_web_fetch(url="https://example.com/")
            second = web_fetch_tool.run_web_fetch(url="https://example.com/")

        self.assertEqual("error", first["status"])
        self.assertEqual("EMPTY_CONTENT", first["error"]["code"])
        self.assertEqual(first["error"]["code"], second["error"]["code"])
        self.assertEqual([], list(Path(tmp).glob("*.json")))

    def test_navigation_only_html_returns_empty_content(self):
        response = FetchResponse(
            requested_url="https://example.com/",
            final_url="https://example.com/",
            status_code=200,
            headers={"content-type": "text/html; charset=utf-8"},
            body=(
                b"<html><body><nav><a href='/a'>A</a><a href='/b'>B</a>"
                b"<a href='/c'>C</a></nav><footer><a href='/p'>Privacy</a></footer>"
                b"</body></html>"
            ),
            encoded_bytes=140,
            decoded_bytes=140,
        )
        with patch.object(web_fetch_tool, "fetch_with_redirects", return_value=response):
            result = web_fetch_tool.run_web_fetch(url="https://example.com/")

        self.assertEqual("error", result["status"])
        self.assertEqual("EMPTY_CONTENT", result["error"]["code"])

    def test_audit_records_empty_content_even_with_returned_response(self):
        response = FetchResponse(
            requested_url="https://example.com/",
            final_url="https://example.com/",
            status_code=200,
            headers={"content-type": "text/html; charset=utf-8"},
            body=b"<html><body></body></html>",
            encoded_bytes=28,
            decoded_bytes=28,
        )
        with tempfile.TemporaryDirectory() as tmp:
            audit_path = Path(tmp) / "audit.jsonl"
            with patch.dict(
                os.environ, {"GA_WEBFETCH_AUDIT_PATH": str(audit_path)}, clear=False
            ), patch.object(web_fetch_tool, "fetch_with_redirects", return_value=response):
                result = web_fetch_tool.run_web_fetch(url="https://example.com/")

            self.assertEqual("EMPTY_CONTENT", result["error"]["code"])
            audit = json.loads(audit_path.read_text(encoding="utf-8"))
            self.assertEqual("EMPTY_CONTENT", audit["error_code"])
            self.assertEqual(200, audit["http_status"])
            self.assertEqual("https://example.com/", audit["final_url"])

    def test_offset_past_end_returns_empty_content(self):
        response = FetchResponse(
            requested_url="https://example.com/",
            final_url="https://example.com/",
            status_code=200,
            headers={"content-type": "text/plain; charset=utf-8"},
            body=b"short body",
            encoded_bytes=10,
            decoded_bytes=10,
        )
        with patch.object(web_fetch_tool, "fetch_with_redirects", return_value=response):
            result = web_fetch_tool.run_web_fetch(url="https://example.com/", offset=100)

        self.assertEqual("error", result["status"])
        self.assertEqual("EMPTY_CONTENT", result["error"]["code"])


if __name__ == "__main__":
    unittest.main()
