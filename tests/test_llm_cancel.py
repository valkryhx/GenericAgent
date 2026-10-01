import sys
import threading
import time
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from llmcore import LLMSession  # noqa: E402


class BlockingStreamResponse:
    status_code = 200
    headers = {}

    def __init__(self):
        self.iter_started = threading.Event()
        self.closed = threading.Event()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def close(self):
        self.closed.set()

    def iter_lines(self):
        self.iter_started.set()
        self.closed.wait(timeout=10)
        return iter(())


class RetryResponse:
    status_code = 429
    headers = {"retry-after": "30"}
    text = "slow down"

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


class StubHttpSession:
    """Stands in for the pooled requests.Session inside llmcore.

    The cancel tests are about cancellation semantics, not the HTTP layer, so
    they replace the pooled transport rather than the raw requests.post call.
    """

    def __init__(self, impl):
        self._impl = impl
        self.calls = 0
        self.proxies = None
        self.verify = True

    def post(self, url, **kwargs):
        self.calls += 1
        return self._impl()


@contextmanager
def patch_transport(impl):
    stub = StubHttpSession(impl)
    with patch("llmcore._get_http_session", return_value=stub):
        yield stub


def make_session(**overrides):
    cfg = {
        "apikey": "test-key",
        "apibase": "https://example.invalid",
        "model": "gpt-test",
        "stream": True,
        "max_retries": 0,
    }
    cfg.update(overrides)
    return LLMSession(cfg)


class LLMCancelTest(unittest.TestCase):
    def test_cancel_current_request_stops_blocked_stream_reader(self):
        sess = make_session(read_timeout=120)
        response = BlockingStreamResponse()
        chunks = []
        errors = []

        def consume():
            try:
                chunks.extend(sess.raw_ask([{"role": "user", "content": "hi"}]))
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        with patch_transport(lambda: response):
            t = threading.Thread(target=consume, daemon=True)
            t.start()
            self.assertTrue(response.iter_started.wait(timeout=1))

            sess.cancel_current_request()

            t.join(timeout=1)

        self.assertFalse(t.is_alive())
        self.assertTrue(response.closed.is_set())
        self.assertEqual([], chunks)
        self.assertEqual([], errors)

    def test_reset_cancel_does_not_revive_old_pending_request(self):
        sess = make_session(read_timeout=120)
        response = BlockingStreamResponse()
        post_can_return = threading.Event()
        errors = []

        def fake_post(*_args, **_kwargs):
            post_can_return.wait(timeout=10)
            return response

        def consume():
            try:
                list(sess.raw_ask([{"role": "user", "content": "hi"}]))
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        with patch_transport(fake_post):
            t = threading.Thread(target=consume, daemon=True)
            t.start()
            time.sleep(0.05)

            sess.cancel_current_request()
            sess.reset_cancel()
            post_can_return.set()

            t.join(timeout=1)

        self.assertFalse(t.is_alive())
        self.assertTrue(response.closed.is_set())
        self.assertEqual([], errors)

    def test_cancel_current_request_interrupts_retry_delay(self):
        sess = make_session(max_retries=1)
        errors = []
        started = threading.Event()

        def fake_post(*_args, **_kwargs):
            started.set()
            return RetryResponse()

        def consume():
            try:
                list(sess.raw_ask([{"role": "user", "content": "hi"}]))
            except Exception as exc:  # pragma: no cover - asserted below
                errors.append(exc)

        with patch_transport(fake_post) as post:
            t = threading.Thread(target=consume, daemon=True)
            t.start()
            self.assertTrue(started.wait(timeout=1))
            time.sleep(0.05)

            sess.cancel_current_request()

            t.join(timeout=1)

        self.assertFalse(t.is_alive())
        self.assertEqual(1, post.calls)
        self.assertEqual([], errors)


if __name__ == "__main__":
    unittest.main()
