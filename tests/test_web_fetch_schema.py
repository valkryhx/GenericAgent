import json
import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


class WebFetchSchemaTest(unittest.TestCase):
    def test_schema_contract_in_both_languages(self):
        for name in ("tools_schema.json", "tools_schema_cn.json"):
            with self.subTest(name=name):
                data = json.loads((REPO_ROOT / "assets" / name).read_text(encoding="utf-8"))
                entries = [item for item in data if item["function"]["name"] == "web_fetch"]
                self.assertEqual(1, len(entries))
                params = entries[0]["function"]["parameters"]
                self.assertEqual(["url"], params["required"])
                self.assertEqual({"url", "prompt", "offset"}, set(params["properties"]))
                self.assertNotIn("proxy", params["properties"])
                self.assertNotIn("direct", params["properties"])
                self.assertNotIn("vpn", params["properties"])

                code_run = next(
                    item for item in data if item["function"]["name"] == "code_run"
                )
                self.assertNotIn("additionalProperties", code_run["function"]["parameters"])


if __name__ == "__main__":
    unittest.main()
