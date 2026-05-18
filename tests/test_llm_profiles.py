import unittest

from llmcore import expand_llm_profile_configs


class LlmProfileConfigTests(unittest.TestCase):
    def test_keeps_legacy_configs(self):
        source = {
            "native_oai_config": {
                "session": "NativeOAISession",
                "name": "legacy",
                "model": "gpt-test",
            }
        }

        expanded = expand_llm_profile_configs(source)

        self.assertIn("native_oai_config", expanded)
        self.assertEqual(expanded["native_oai_config"]["name"], "legacy")

    def test_expands_profile_list_to_synthetic_configs(self):
        source = {
            "llm_profile_configs": [
                {
                    "key": "native_gpt55_config",
                    "session": "NativeOAISession",
                    "name": "gpt-native",
                    "model": "gpt-5.5",
                    "apikey": "test-key",
                    "apibase": "https://example.test/v1",
                },
                {
                    "key": "native_kimi_config",
                    "session": "NativeOAISession",
                    "name": "kimi-native",
                    "model": "moonshotai/kimi-k2.6",
                    "apikey": "test-key-2",
                    "apibase": "https://example2.test/v1",
                },
            ]
        }

        expanded = expand_llm_profile_configs(source)

        self.assertEqual(expanded["native_gpt55_config"]["model"], "gpt-5.5")
        self.assertEqual(expanded["native_kimi_config"]["name"], "kimi-native")
        self.assertNotIn("key", expanded["native_gpt55_config"])

    def test_invalid_profile_key_is_ignored(self):
        source = {
            "llm_profile_configs": [
                {
                    "key": "bad",
                    "session": "NativeOAISession",
                    "name": "bad",
                    "model": "gpt-test",
                }
            ]
        }

        expanded = expand_llm_profile_configs(source)

        self.assertNotIn("bad", expanded)

    def test_duplicate_profile_does_not_override_existing_config(self):
        source = {
            "native_oai_config": {"name": "legacy", "model": "gpt-old"},
            "llm_profile_configs": [
                {
                    "key": "native_oai_config",
                    "name": "new",
                    "model": "gpt-new",
                }
            ],
        }

        expanded = expand_llm_profile_configs(source)

        self.assertEqual(expanded["native_oai_config"]["name"], "legacy")


if __name__ == "__main__":
    unittest.main()
