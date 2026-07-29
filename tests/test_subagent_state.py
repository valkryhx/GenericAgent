import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


import subagent_state  # noqa: E402
from subagent_state import append_jsonl_event, atomic_write_json, read_json_or_none, sha256_file  # noqa: E402


class SubagentStateTest(unittest.TestCase):
    def test_atomic_write_json_replaces_file_with_complete_json(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.json"

            atomic_write_json(path, {"turn_status": "running", "process_status": "alive"})
            atomic_write_json(path, {"turn_status": "completed", "process_status": "waiting_reply"})

            self.assertEqual(
                read_json_or_none(path),
                {"turn_status": "completed", "process_status": "waiting_reply"},
            )
            self.assertEqual(list(Path(td).glob("*.tmp")), [])

    def test_atomic_write_json_retries_transient_replace_permission_error(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            real_replace = subagent_state.os.replace
            replace_calls = []

            def flaky_replace(src, dst):
                replace_calls.append((src, dst))
                if len(replace_calls) == 1:
                    raise PermissionError("temporarily locked")
                return real_replace(src, dst)

            with patch.object(subagent_state.os, "name", "nt"):
                with patch.object(subagent_state.time, "sleep") as sleep_mock:
                    with patch.object(subagent_state.os, "replace", side_effect=flaky_replace):
                        atomic_write_json(path, {"turn_status": "completed"})

            self.assertEqual(read_json_or_none(path), {"turn_status": "completed"})
            self.assertEqual(len(replace_calls), 2)
            sleep_mock.assert_called_once_with(subagent_state._WINDOWS_REPLACE_RETRY_DELAYS[0])
            self.assertEqual(list(Path(td).glob("*.tmp")), [])

    def test_append_jsonl_event_writes_one_json_object_per_line(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "events.jsonl"

            append_jsonl_event(path, {"type": "agent_started", "task_name": "demo"})
            append_jsonl_event(path, {"type": "turn_completed", "task_name": "demo"})

            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual([row["type"] for row in rows], ["agent_started", "turn_completed"])
            self.assertTrue(all(row["schema_version"] == 1 for row in rows))
            self.assertTrue(all("ts" in row for row in rows))

    def test_sha256_file_hashes_file_content(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "output.txt"
            path.write_text("final\n\n[ROUND END]\n", encoding="utf-8")

            self.assertEqual(
                sha256_file(path),
                "39e5d7655411265b549d8033f7632529a12c84ecf19dec187d048786bdbe26d4",
            )


if __name__ == "__main__":
    unittest.main()
