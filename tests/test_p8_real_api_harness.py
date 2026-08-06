from __future__ import annotations

import tempfile
import threading
import time
import unittest
from pathlib import Path

from tests import p8_real_api_e2e


PROFILE_YAML = """
providers:
  relay:
    wire_api: openai_responses
    base_url: https://example.invalid/v1
    api_key: test-key
models:
  luna-model:
    provider: relay
profiles:
  luna:
    model: luna-model
active_profile: luna
"""


class P8RealApiHarnessTest(unittest.TestCase):
    def test_secret_scanner_ignores_safe_fixture_fields_and_compiled_artifacts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "safe-transcript.json").write_text(
                "api_key=demo token=placeholder secret=example password=test "
                "x-api-key: demo https://example.test/?api_key=placeholder&token=example",
                encoding="utf-8",
            )
            (root / "compiled.pyc").write_bytes(
                b"Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456"
            )
            (root / "compiled.pyo").write_bytes(
                b"sk-proj-abcdefghijklmnopQRST"
            )

            hits = p8_real_api_e2e.scan_for_secret_material(root)

        self.assertEqual([], hits)

    def test_secret_scanner_blocks_high_confidence_formats(self):
        fixtures = {
            "anthropic.txt": "sk-ant-api03-abcdefghijklmnop",
            "openai.txt": "sk-proj-abcdefghijklmnopQRST",
            "github-pat.txt": "github_pat_1234567890abcdefghijklmnop",
            "github-token.txt": "ghp_1234567890abcdefghijklmnop",
            "jwt.txt": "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ1c2VyIn0.signature",
            "bearer.txt": "Authorization: Bearer abcdefghijklmnopqrstuvwxyz123456",
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, value in fixtures.items():
                (root / name).write_text(value, encoding="utf-8")

            hits = p8_real_api_e2e.scan_for_secret_material(root)

        self.assertEqual(
            {
                ("anthropic.txt", "anthropic_key"),
                ("openai.txt", "openai_key"),
                ("github-pat.txt", "github_pat"),
                ("github-token.txt", "github_token"),
                ("jwt.txt", "jwt"),
                ("bearer.txt", "bearer"),
            },
            {(hit["file"], hit["pattern"]) for hit in hits},
        )

    def test_sanitizer_keeps_generic_redaction_separate_from_blocking_scan(self):
        fixture = "api_key=demo token=placeholder secret=example password=test"

        sanitized = p8_real_api_e2e.sanitize(fixture)
        self.assertNotEqual(fixture, sanitized)
        self.assertIn("[REDACTED]", sanitized)

    def test_p8_uses_high_confidence_scanner_mode(self):
        self.assertEqual("high-confidence-only", p8_real_api_e2e.SECRET_SCANNER_MODE)

    def test_profile_metadata_resolves_yaml_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            config_path = Path(tmp) / "llm.yaml"
            config_path.write_text(PROFILE_YAML, encoding="utf-8")

            profile = p8_real_api_e2e._resolve_profile_metadata("luna", config_path=str(config_path))

        self.assertEqual(
            {
                "configName": "luna",
                "name": "luna",
                "model": "luna-model",
                "apiMode": "responses",
                "source": "llm.yaml",
            },
            profile,
        )

    def test_wait_for_runtime_thread_allows_killed_event_to_be_written(self):
        observed = []

        def finish_runtime_cleanup():
            time.sleep(0.02)
            observed.append("workflow_killed")

        thread = threading.Thread(target=finish_runtime_cleanup)
        thread.start()

        joined = p8_real_api_e2e._wait_for_runtime_thread(thread, timeout_seconds=1.0)

        self.assertTrue(joined)
        self.assertEqual(["workflow_killed"], observed)


if __name__ == "__main__":
    unittest.main()
