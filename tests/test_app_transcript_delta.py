import tempfile
import unittest
from unittest.mock import patch


class AppTranscriptDeltaTest(unittest.TestCase):
    def test_v2_turn_anchor_is_history_length_without_deep_snapshot(self):
        from genericagent.core import app as app_module
        from genericagent.core import transcripts

        agent = app_module.GenericAgent.__new__(app_module.GenericAgent)
        with tempfile.TemporaryDirectory() as tmp:
            path = transcripts.create_session(root=tmp, session_id="session_delta")
            agent.session_path = path
            agent.llmclient = type("Client", (), {
                "backend": type("Backend", (), {"history": [{"role": "user", "content": "x"}]})()
            })()

            with patch.object(
                transcripts,
                "current_backend_history",
                side_effect=AssertionError("v2 must not deep-copy the full history"),
            ):
                self.assertEqual(1, app_module.transcript_delta_anchor(agent))

        self.assertEqual([{"role": "user", "content": "x"}], agent.llmclient.backend.history)


if __name__ == "__main__":
    unittest.main()
