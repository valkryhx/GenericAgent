import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from genericagent.tools.web_fetch.models import FetchError, WebFetchRequest  # noqa: E402


class WebFetchModelsTest(unittest.TestCase):
    def test_request_preserves_url_prompt_and_offset(self):
        request = WebFetchRequest(
            url="https://example.com/page",
            prompt="summarize it",
            offset=12,
        )

        self.assertEqual("https://example.com/page", request.url)
        self.assertEqual("summarize it", request.prompt)
        self.assertEqual(12, request.offset)

    def test_fetch_error_preserves_code_and_details(self):
        error = FetchError(
            "HOST_BLOCKED",
            "host is not allowed",
            details={"host": "localhost"},
        )

        self.assertEqual("HOST_BLOCKED", error.code)
        self.assertEqual("host is not allowed", str(error))
        self.assertEqual({"host": "localhost"}, error.details)


if __name__ == "__main__":
    unittest.main()
