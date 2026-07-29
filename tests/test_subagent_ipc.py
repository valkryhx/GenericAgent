import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from subagent_ipc import normalize_ipc_metadata  # noqa: E402


class SubagentIpcTest(unittest.TestCase):
    def test_realtime_mode_becomes_effective_when_channel_opens(self):
        opened = []

        class FakeChannel:
            def __init__(self):
                self.address = r"\\.\pipe\ga_subagent_fake"

            def start(self):
                opened.append("start")
                return self

            def endpoint(self):
                return {"status": "listening", "address": self.address, "family": "AF_PIPE", "subscriber_count": 0}

        metadata = normalize_ipc_metadata("socket", channel_factory=lambda: FakeChannel())

        self.assertEqual(metadata["ipc_mode"], "socket")
        self.assertEqual(metadata["effective_ipc_mode"], "socket")
        self.assertIsNone(metadata["ipc_fallback_reason"])
        self.assertEqual(metadata["ipc_endpoint"]["status"], "listening")
        self.assertEqual(metadata["ipc_endpoint"]["address"], r"\\.\pipe\ga_subagent_fake")
        self.assertIsNotNone(metadata["channel"])
        self.assertEqual(opened, ["start"])

    def test_realtime_mode_falls_back_when_channel_cannot_open(self):
        def broken_factory():
            raise PermissionError("access denied")

        metadata = normalize_ipc_metadata("event_server", channel_factory=broken_factory)

        self.assertEqual(metadata["ipc_mode"], "event_server")
        self.assertEqual(metadata["effective_ipc_mode"], "file")
        self.assertIn("durable file event bus", metadata["ipc_fallback_reason"])
        self.assertIn("PermissionError", metadata["ipc_fallback_reason"])
        self.assertIsNone(metadata["channel"])

    def test_file_mode_is_effective_without_fallback(self):
        metadata = normalize_ipc_metadata("file")

        self.assertEqual(metadata["ipc_mode"], "file")
        self.assertEqual(metadata["effective_ipc_mode"], "file")
        self.assertIsNone(metadata["ipc_fallback_reason"])

    def test_realtime_modes_fall_back_to_durable_file_bus(self):
        metadata = normalize_ipc_metadata("socket")

        self.assertEqual(metadata["ipc_mode"], "socket")
        self.assertEqual(metadata["effective_ipc_mode"], "file")
        self.assertIn("durable file event bus", metadata["ipc_fallback_reason"])

    def test_unknown_mode_falls_back_to_file(self):
        metadata = normalize_ipc_metadata("websocket")

        self.assertEqual(metadata["ipc_mode"], "websocket")
        self.assertEqual(metadata["effective_ipc_mode"], "file")
        self.assertIn("unknown", metadata["ipc_fallback_reason"])


if __name__ == "__main__":
    unittest.main()
