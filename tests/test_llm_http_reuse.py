import sys
import threading
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


import llmcore  # noqa: E402


class _Backend:
    """Minimal stand-in for the session object _stream_with_retry uses."""

    def __init__(self):
        self._cancel_event = threading.Event()
        self._active_response = None
        self._request_lock = threading.Lock()
        self.max_retries = 0
        self.stream = True
        self.connect_timeout = 5
        self.read_timeout = 30
        self.proxies = None
        self.verify = True

    def _close_active_response(self):
        pass

    def _set_active_response(self, r):
        self._active_response = r

    def _clear_active_response(self, r):
        if self._active_response is r:
            self._active_response = None


class _FakeResponse:
    """Context manager that records the session it was opened from."""

    def __init__(self, status_code=200, lines=None):
        self.status_code = status_code
        self.headers = {}
        self._lines = lines or []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_lines(self):
        for line in self._lines:
            yield line


class _RecordingSession:
    def __init__(self):
        self.posts = []
        self.closed = 0
        self.mounted = []
        self.proxies = None
        self.verify = True

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return _FakeResponse()

    def mount(self, prefix, adapter):
        self.mounted.append(prefix)

    def close(self):
        self.closed += 1


class HttpConnectionReuseTest(unittest.TestCase):
    """A backend must reuse one pooled Session instead of a per-call session."""

    def setUp(self):
        self.sessions = []

        def factory():
            session = _RecordingSession()
            self.sessions.append(session)
            return session

        patcher = mock.patch.object(llmcore.requests, "Session", side_effect=factory)
        self.addCleanup(patcher.stop)
        patcher.start()

    def _parse(self, response):
        if False:
            yield ""
        return []

    def test_pooled_session_is_created_once_and_reused(self):
        backend = _Backend()
        for _ in range(3):
            list(llmcore._stream_with_retry(backend, "http://x/v1/responses", {}, {}, self._parse))

        self.assertEqual(len(self.sessions), 1, "expected one pooled Session for the backend")
        self.assertEqual(len(self.sessions[0].posts), 3)

    def test_pooled_session_mounts_pooled_adapters(self):
        backend = _Backend()
        session = llmcore._get_http_session(backend)
        self.assertEqual(session.mounted, ["http://", "https://"])
        self.assertIs(llmcore._get_http_session(backend), session)

    def test_adapter_does_not_retry_on_its_own(self):
        # _stream_with_retry owns the retry policy; a retrying adapter would
        # silently double the request count.
        backend = _Backend()
        captured = {}
        real_adapter = llmcore.requests.adapters.HTTPAdapter

        def adapter_factory(**kwargs):
            captured.update(kwargs)
            return real_adapter(**kwargs)

        with mock.patch.object(llmcore.requests.adapters, "HTTPAdapter", side_effect=adapter_factory):
            llmcore._get_http_session(backend)

        self.assertEqual(captured.get("max_retries"), 0)

    def test_per_backend_sessions_are_independent(self):
        first, second = _Backend(), _Backend()
        self.assertIsNot(llmcore._get_http_session(first), llmcore._get_http_session(second))

    def test_close_http_session_releases_and_forgets(self):
        backend = _Backend()
        session = llmcore._get_http_session(backend)
        llmcore.close_http_session(backend)
        self.assertEqual(session.closed, 1)
        self.assertIsNone(getattr(backend, "_http_session", None))

    def test_close_http_session_is_safe_without_a_session(self):
        backend = _Backend()
        llmcore.close_http_session(backend)  # must not raise

    def test_proxies_and_verify_are_applied_per_request(self):
        backend = _Backend()
        backend.proxies = {"https": "http://proxy:8080"}
        backend.verify = False
        list(llmcore._stream_with_retry(backend, "http://x/v1/responses", {}, {}, self._parse))

        session = self.sessions[0]
        self.assertEqual(session.proxies, backend.proxies)
        self.assertFalse(session.verify)


if __name__ == "__main__":
    unittest.main()
