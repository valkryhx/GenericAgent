import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from genericagent.tools.web_fetch.audit import build_audit_record, redact_url, write_audit_record  # noqa: E402


class WebFetchAuditTest(unittest.TestCase):
    def test_record_contains_egress_and_no_body_or_credentials(self):
        record = build_audit_record(
            url="https://example.com/path?token=secret#frag",
            final_url="https://example.com/path?token=secret#frag",
            status="success",
            egress={"mode": "transparent", "security_assurance": "basic"},
            http_status=200,
            encoded_bytes=10,
            decoded_bytes=20,
            duration_ms=12,
            body="FULL PAGE SECRET",
            headers={"Authorization": "Bearer secret", "Set-Cookie": "session=secret"},
            cache_status="miss",
        )

        dumped = json.dumps(record, ensure_ascii=False)
        self.assertNotIn("FULL PAGE SECRET", dumped)
        self.assertNotIn("Bearer secret", dumped)
        self.assertNotIn("session=secret", dumped)
        self.assertNotIn("token=secret", dumped)
        self.assertEqual("transparent", record["egress_mode"])
        self.assertEqual("basic", record["security_assurance"])

    def test_query_is_redacted_in_both_urls(self):
        self.assertEqual(
            "https://example.com/path?redacted=1",
            redact_url("https://example.com/path?token=secret&b=2"),
        )

    def test_audit_writer_is_jsonl_and_utf8(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "audit.jsonl"
            record = build_audit_record(
                url="https://example.com/",
                final_url="https://example.com/",
                status="success",
                egress={"mode": "transparent", "security_assurance": "basic"},
            )
            write_audit_record(record, path)
            loaded = json.loads(path.read_text(encoding="utf-8").strip())
            self.assertEqual("web_fetch", loaded["type"])


if __name__ == "__main__":
    unittest.main()
