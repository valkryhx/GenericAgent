"""zhanlu-ds 这类网关的 502 / 504 是偶发故障：必须退避重试，重试用尽才报错。

真机冒烟里遇到过一次 502（provider 侧），当时 GA 已经按 _RETRYABLE 退避了 3 次。
这里把这条行为钉住，免得以后把 502/504 从重试名单里删掉。
"""

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from genericagent.llm.core import LLMSession, _stream_with_retry  # noqa: E402


class ErrorResponse:
    def __init__(self, status_code, body="upstream thing"):
        self.status_code = status_code
        self.headers = {"retry-after": "0"}
        self.text = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


class OkResponse:
    status_code = 200
    headers = {}
    text = ""

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


class StubHttpSession:
    def __init__(self, outcomes):
        self._outcomes = list(outcomes)
        self.calls = 0
        self.proxies = None
        self.verify = True

    def post(self, url, **kwargs):
        outcome = self._outcomes[min(self.calls, len(self._outcomes) - 1)]
        self.calls += 1
        return outcome


def make_session(**overrides):
    cfg = {
        "apikey": "test-key",
        "apibase": "https://example.invalid",
        "model": "gpt-test",
        "stream": True,
        "max_retries": 2,
    }
    cfg.update(overrides)
    return LLMSession(cfg)


def _parse(_response):
    yield "hello"
    return []


def _drain(sess, stub):
    with patch("genericagent.llm.core._get_http_session", return_value=stub), \
            patch("genericagent.llm.core.time.sleep", lambda *_: None):
        return list(_stream_with_retry(sess, "https://example.invalid", {}, {}, _parse))


def _drain_with_status(sess, stub):
    """Collect the retry banner events alongside the chunks."""
    events = []
    sess.on_retry_status = events.append
    return _drain(sess, stub), events


class TransientGatewayRetryTest(unittest.TestCase):
    def test_502_is_retried_then_succeeds(self):
        stub = StubHttpSession([ErrorResponse(502, "<html>502 Bad Gateway</html>"), OkResponse()])
        chunks = _drain(make_session(max_retries=2), stub)
        self.assertEqual(2, stub.calls, "502 should have been retried once")
        self.assertEqual(["hello"], chunks)
        self.assertFalse(any("!!!Error" in str(chunk) for chunk in chunks))

    def test_504_is_retried_then_succeeds(self):
        stub = StubHttpSession([ErrorResponse(504), OkResponse()])
        chunks = _drain(make_session(max_retries=2), stub)
        self.assertEqual(2, stub.calls)
        self.assertEqual(["hello"], chunks)

    def test_exhausted_retries_surface_the_http_error_once(self):
        stub = StubHttpSession([ErrorResponse(502, "<html>nginx</html>")])
        chunks = _drain(make_session(max_retries=2), stub)
        self.assertEqual(3, stub.calls, "max_retries=2 means three attempts in total")
        self.assertTrue(any("!!!Error: HTTP 502" in str(chunk) for chunk in chunks))

    def test_non_retryable_status_is_not_retried(self):
        stub = StubHttpSession([ErrorResponse(403, "forbidden"), OkResponse()])
        chunks = _drain(make_session(max_retries=2), stub)
        self.assertEqual(1, stub.calls, "403 must not be retried")
        self.assertTrue(any("!!!Error: HTTP 403" in str(chunk) for chunk in chunks))


class RetryStatusBannerTest(unittest.TestCase):
    """断链横幅：重试期间给前端一条状态，重连成功后立刻撤掉。

    用户的原话是「不要让这些报错很突兀地显示在 ux 上」——所以断言不只是
    "有 retrying 事件"，还要断言成功那一路**没有** retrying，以及恢复后
    一定有一条 recovered 把横幅清掉。
    """

    def test_502_emits_retrying_then_recovered(self):
        stub = StubHttpSession([ErrorResponse(502), OkResponse()])
        chunks, events = _drain_with_status(make_session(max_retries=2), stub)
        self.assertEqual(["hello"], chunks)
        kinds = [event["kind"] for event in events]
        self.assertEqual(["retrying", "recovered"], kinds, f"unexpected banner sequence: {events}")
        self.assertEqual(502, events[0]["status"])
        self.assertEqual(1, events[0]["attempt"])
        self.assertEqual(2, events[0]["max_retries"])

    def test_recovered_comes_before_the_first_visible_token(self):
        """横幅必须先撤、再出字，否则新内容会被旧的 "重试中" 压一帧。"""
        stub = StubHttpSession([ErrorResponse(504), OkResponse()])
        order = []
        sess = make_session(max_retries=2)
        sess.on_retry_status = lambda payload: order.append(payload["kind"])
        with patch("genericagent.llm.core._get_http_session", return_value=stub), \
                patch("genericagent.llm.core.time.sleep", lambda *_: None):
            gen = _stream_with_retry(sess, "https://example.invalid", {}, {}, _parse)
            for _chunk in gen:
                order.append("token")
        self.assertEqual(["retrying", "recovered", "token"], order)
        self.assertEqual(2, stub.calls)

    def test_clean_response_emits_no_banner(self):
        stub = StubHttpSession([OkResponse()])
        chunks, events = _drain_with_status(make_session(max_retries=2), stub)
        self.assertEqual(["hello"], chunks)
        self.assertEqual([], events, "a first-try success must not paint any retry banner")

    def test_exhausted_retries_never_claim_recovery(self):
        """重试用尽时不能发 recovered —— 那会让横幅先消失再弹错误，像是自愈了。"""
        stub = StubHttpSession([ErrorResponse(502)])
        chunks, events = _drain_with_status(make_session(max_retries=1), stub)
        self.assertEqual(["retrying"], [event["kind"] for event in events])
        self.assertTrue(any("!!!Error: HTTP 502" in str(chunk) for chunk in chunks))


if __name__ == "__main__":
    unittest.main()
