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
