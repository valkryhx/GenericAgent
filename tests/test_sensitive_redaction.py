import json
import unittest

from sensitive_redaction import REDACTION, redact_sensitive_text, sanitize


class SensitiveRedactionTest(unittest.TestCase):
    def test_redact_sensitive_text_covers_header_tokens_query_keys_and_cookies(self):
        secret_values = [
            "sk-test-secret",
            "bearer-secret",
            "xkey-secret",
            "query-secret",
            "access-secret",
            "jwt-secret.header.payload",
            "cookie_secret",
            "set_cookie_secret",
        ]
        text = (
            "HTTP 500 request_id=req_123 "
            "Authorization: Bearer sk-test-secret "
            "Bearer bearer-secret "
            "x-api-key: xkey-secret "
            "https://example.test/path?api_key=query-secret&access_token=access-secret "
            "jwt=jwt-secret.header.payload "
            "Cookie: sid=cookie_secret; theme=dark "
            "Set-Cookie: session=set_cookie_secret; Path=/"
        )

        redacted = redact_sensitive_text(text)

        self.assertIn("HTTP 500", redacted)
        self.assertIn("request_id=req_123", redacted)
        self.assertIn(REDACTION, redacted)
        for secret in secret_values:
            self.assertNotIn(secret, redacted)

    def test_sanitize_redacts_nested_sensitive_keys_and_container_values(self):
        value = {
            "apiKey": "api-key-secret",
            "nested": {
                "clientSecret": "client-secret-value",
                "message": "Bearer bearer-nested-secret request_id=req_123",
            },
            "items": ["token=list-secret", {"password": "password-secret"}],
            "tupleValue": ("Cookie: sid=tuple-cookie-secret",),
        }

        sanitized = sanitize(value)
        serialized = json.dumps(sanitized, ensure_ascii=False)

        self.assertEqual(REDACTION, sanitized["apiKey"])
        self.assertEqual(REDACTION, sanitized["nested"]["clientSecret"])
        self.assertEqual(REDACTION, sanitized["items"][1]["password"])
        self.assertIn("request_id=req_123", serialized)
        self.assertIn(REDACTION, serialized)
        for secret in [
            "api-key-secret",
            "client-secret-value",
            "bearer-nested-secret",
            "list-secret",
            "password-secret",
            "tuple-cookie-secret",
        ]:
            self.assertNotIn(secret, serialized)


if __name__ == "__main__":
    unittest.main()
