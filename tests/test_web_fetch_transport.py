import gzip
import io
import sys
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from genericagent.tools.web_fetch.models import FetchError  # noqa: E402
from genericagent.tools.web_fetch import transport  # noqa: E402
from genericagent.tools.web_fetch.transport import (  # noqa: E402
    FetchLimits,
    fetch_once,
    fetch_with_redirects,
)


class FakeResponse:
    def __init__(self, status=200, headers=None, chunks=(), url="https://example.com/"):
        self.status_code = status
        self.headers = headers or {}
        self._chunks = list(chunks)
        self.url = url
        self.closed = False
        self.encoding = None

    def iter_content(self, chunk_size=65536):
        for chunk in self._chunks:
            if isinstance(chunk, Exception):
                raise chunk
            yield chunk

    def close(self):
        self.closed = True


class FakeSession:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.closed = False

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def close(self):
        self.closed = True


class WebFetchTransportTest(unittest.TestCase):
    def test_uses_streaming_and_disables_automatic_redirects(self):
        response = FakeResponse(chunks=[b"hello"], headers={"Content-Type": "text/plain"})
        session = FakeSession([response])

        result = fetch_once(session, "https://example.com/")

        self.assertEqual(b"hello", result.body)
        _, kwargs = session.calls[0]
        self.assertTrue(kwargs["stream"])
        self.assertFalse(kwargs["allow_redirects"])
        self.assertTrue(kwargs["verify"])
        self.assertTrue(response.closed)

    def test_content_length_above_limit_is_rejected_before_reading(self):
        response = FakeResponse(
            headers={"Content-Type": "text/plain", "Content-Length": "999999"},
            chunks=[b"x"],
        )
        with self.assertRaises(FetchError) as raised:
            fetch_once(
                FakeSession([response]),
                "https://example.com/",
                limits=FetchLimits(max_encoded_bytes=100),
            )

        self.assertEqual("BODY_TOO_LARGE", raised.exception.code)
        self.assertTrue(response.closed)

    def test_streamed_body_above_limit_is_rejected(self):
        response = FakeResponse(
            headers={"Content-Type": "text/plain"},
            chunks=[b"a" * 10, b"b" * 10],
        )
        with self.assertRaises(FetchError) as raised:
            fetch_once(
                FakeSession([response]),
                "https://example.com/",
                limits=FetchLimits(max_encoded_bytes=15),
            )

        self.assertEqual("BODY_TOO_LARGE", raised.exception.code)
        self.assertTrue(response.closed)

    def test_headers_above_limit_are_rejected(self):
        response = FakeResponse(
            headers={"Content-Type": "text/plain", "X-Large": "x" * 20000},
            chunks=[b"ok"],
        )
        with self.assertRaises(FetchError) as raised:
            fetch_once(FakeSession([response]), "https://example.com/")

        self.assertEqual("HEADER_TOO_LARGE", raised.exception.code)

    def test_gzip_is_decoded_with_decoded_size_limit(self):
        raw = gzip.compress(b"hello" * 100)
        response = FakeResponse(
            headers={"Content-Type": "text/plain", "Content-Encoding": "gzip"},
            chunks=[raw],
        )

        result = fetch_once(
            FakeSession([response]),
            "https://example.com/",
            limits=FetchLimits(max_decoded_bytes=500),
        )

        self.assertEqual(("hello" * 100).encode(), result.body)
        self.assertFalse(result.truncated)

    def test_compression_bomb_is_rejected_by_decoded_limit(self):
        raw = gzip.compress(b"a" * 10000)
        response = FakeResponse(
            headers={"Content-Type": "text/plain", "Content-Encoding": "gzip"},
            chunks=[raw],
        )

        with self.assertRaises(FetchError) as raised:
            fetch_once(
                FakeSession([response]),
                "https://example.com/",
                limits=FetchLimits(max_decoded_bytes=100),
            )

        self.assertEqual("DECODED_BODY_TOO_LARGE", raised.exception.code)

    def test_invalid_compressed_body_has_stable_error(self):
        response = FakeResponse(
            headers={"Content-Type": "text/plain", "Content-Encoding": "gzip"},
            chunks=[bytes((0x1F, 0x8B)) + b"truncated"],
        )
        with self.assertRaises(FetchError) as raised:
            fetch_once(FakeSession([response]), "https://example.com/")

        self.assertEqual("DECODE_FAILED", raised.exception.code)

    def test_http_error_status_is_returned_as_typed_error(self):
        response = FakeResponse(status=404, headers={"Content-Type": "text/plain"}, chunks=[b"missing"])
        with self.assertRaises(FetchError) as raised:
            fetch_once(FakeSession([response]), "https://example.com/")

        self.assertEqual("HTTP_STATUS", raised.exception.code)
        self.assertEqual(404, raised.exception.details["status"])

    def test_same_origin_redirect_is_followed_and_recorded(self):
        first = FakeResponse(
            status=302,
            headers={"Location": "/next"},
            url="https://example.com/start",
        )
        second = FakeResponse(
            status=200,
            headers={"Content-Type": "text/plain"},
            chunks=[b"next"],
            url="https://example.com/next",
        )
        session = FakeSession([first, second])

        result = fetch_with_redirects(session, "https://example.com/start")

        self.assertEqual("https://example.com/next", result.final_url)
        self.assertEqual(["https://example.com/start", "https://example.com/next"], session.calls and [c[0] for c in session.calls])
        self.assertEqual(1, len(result.redirects))
        self.assertEqual(b"next", result.body)

    def test_cross_site_redirect_is_reported_without_following(self):
        first = FakeResponse(
            status=302,
            headers={"Location": "https://other.example/"},
            url="https://example.com/start",
        )
        session = FakeSession([first])

        with self.assertRaises(FetchError) as raised:
            fetch_with_redirects(session, "https://example.com/start")

        self.assertEqual("REDIRECT_CROSS_SITE", raised.exception.code)
        self.assertEqual(1, len(session.calls))

    def test_redirect_limit_is_enforced(self):
        responses = [
            FakeResponse(status=302, headers={"Location": f"/{i + 1}"}, url=f"https://example.com/{i}")
            for i in range(5)
        ]
        with self.assertRaises(FetchError) as raised:
            fetch_with_redirects(
                FakeSession(responses),
                "https://example.com/0",
                limits=FetchLimits(max_redirects=1),
            )

        self.assertEqual("REDIRECT_LIMIT", raised.exception.code)

    def test_redirect_to_blocked_scheme_is_revalidated(self):
        response = FakeResponse(
            status=302,
            headers={"Location": "http://example.com/next"},
            url="https://example.com/start",
        )
        with self.assertRaises(FetchError) as raised:
            fetch_with_redirects(FakeSession([response]), "https://example.com/start")

        self.assertEqual("SCHEME_BLOCKED", raised.exception.code)

    def test_cancellation_closes_response(self):
        cancel = threading.Event()

        class CancellingSession(FakeSession):
            def get(self, url, **kwargs):
                self.calls.append((url, kwargs))
                cancel.set()
                return FakeResponse(headers={"Content-Type": "text/plain"}, chunks=[b"late"])

        response = None
        session = CancellingSession([])
    
        original_get = session.get

        def get(url, **kwargs):
            nonlocal response
            response = original_get(url, **kwargs)
            return response

        session.get = get
        with self.assertRaises(FetchError) as raised:
            fetch_once(session, "https://example.com/", stop_signal=cancel)

        self.assertEqual("CANCELLED", raised.exception.code)
        self.assertTrue(response.closed)

    def test_large_declared_body_is_allowed_up_to_limits(self):
        response = FakeResponse(
            headers={"Content-Type": "text/plain", "Content-Length": "300000"},
            chunks=[b"x" * 300000],
        )

        result = fetch_once(FakeSession([response]), "https://example.com/")

        self.assertEqual(300000, len(result.body))

    def test_deadline_is_enforced(self):
        response = FakeResponse(headers={"Content-Type": "text/plain"}, chunks=[b"late"])

        class SlowSession(FakeSession):
            def get(self, url, **kwargs):
                time.sleep(0.03)
                return response

        with patch.object(transport, "time", wraps=time) as clock:
            clock.monotonic.side_effect = [0.0, 0.0, 0.1]
            with self.assertRaises(FetchError) as raised:
                fetch_once(SlowSession([]), "https://example.com/", deadline_seconds=0.01)

        self.assertEqual("DEADLINE", raised.exception.code)

    def test_default_user_agent_is_browser_like_and_not_ga_identified(self):
        session = FakeSession([
            FakeResponse(chunks=[b"ok"], headers={"Content-Type": "text/plain"})
        ])
        with patch.dict("os.environ", {}, clear=True):
            fetch_once(session, "https://example.com/")
        headers = session.calls[0][1]["headers"]
        self.assertIn("Mozilla/5.0", headers["User-Agent"])
        self.assertNotIn("GenericAgent-WebFetch", headers["User-Agent"])

    def test_user_agent_can_be_overridden_from_environment(self):
        session = FakeSession([
            FakeResponse(chunks=[b"hello"], headers={"Content-Type": "text/plain"})
        ])
        with patch.dict("os.environ", {"GA_WEBFETCH_USER_AGENT": "CustomBrowser/1.0"}, clear=False):
            fetch_once(session, "https://example.com/")

        headers = session.calls[0][1]["headers"]
        self.assertEqual("CustomBrowser/1.0", headers["User-Agent"])


if __name__ == "__main__":
    unittest.main()
