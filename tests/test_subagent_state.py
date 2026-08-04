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
from subagent_state import append_jsonl_event, atomic_write_json, atomic_write_text, read_json_or_none, sha256_file  # noqa: E402


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

    def test_read_text_retrying_retries_the_windows_replace_window(self):
        """os.replace and a reader's open race in *both* directions on Windows.

        `_replace_file` already retries when a reader's handle blocks the writer. The reverse also
        happens: a reader that opens during the replace gets PermissionError, not a torn file. That
        surfaced as 62 `read PermissionError`s in the workflow store's concurrency test once the
        truncating write was gone — so an atomic write needs a retrying read to pair with it.
        """
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            path.write_text("done", encoding="utf-8")
            calls = []
            real_read_bytes = Path.read_bytes

            def flaky_read_bytes(self):
                calls.append(self)
                if len(calls) == 1:
                    raise PermissionError("replace in progress")
                return real_read_bytes(self)

            with patch.object(subagent_state.os, "name", "nt"):
                with patch.object(subagent_state.time, "sleep") as sleep_mock:
                    with patch.object(Path, "read_bytes", flaky_read_bytes):
                        self.assertEqual("done", subagent_state.read_text_retrying(path))

            self.assertEqual(2, len(calls))
            sleep_mock.assert_called_once_with(subagent_state._WINDOWS_REPLACE_RETRY_DELAYS[0])

    def test_read_json_retrying_raises_rather_than_hiding_a_corrupt_file(self):
        """Unlike read_json_or_none: a caller deciding whether a run was killed must not get None.

        Swallowing the error there turns a real corrupt row into "no kill on disk", which is the
        exact failure the retry exists to prevent.
        """
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "state.json"
            path.write_text("{not json", encoding="utf-8")

            with self.assertRaises(json.JSONDecodeError):
                subagent_state.read_json_retrying(path)

            atomic_write_json(path, {"status": "killed"})
            self.assertEqual({"status": "killed"}, subagent_state.read_json_retrying(path))

    def test_atomic_write_text_preserves_exact_bytes_without_adding_a_newline(self):
        """The workflow script round-trips through this, so an added newline would change the row.

        `atomic_write_lines` newline-terminates every line, which is right for JSONL bodies and
        wrong for a file whose content is compared verbatim (`load_run` reads `script.js` back into
        `run.script`).
        """
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "script.js"

            atomic_write_text(path, "phase('A')")

            self.assertEqual("phase('A')", path.read_text(encoding="utf-8"))
            self.assertEqual([], list(Path(td).glob("*.tmp")))

    def test_atomic_write_text_replaces_a_longer_file_in_one_step(self):
        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "script.js"

            atomic_write_text(path, "x" * 4000)
            atomic_write_text(path, "short")

            self.assertEqual("short", path.read_text(encoding="utf-8"))

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
