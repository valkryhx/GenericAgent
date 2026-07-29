import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from agent_runtime_models import AgentEvent, AgentStatus, ArtifactRef  # noqa: E402


class AgentRuntimeModelsTest(unittest.TestCase):
    def test_process_subagent_event_converts_to_common_event(self):
        raw = {
            "event_seq": 7,
            "type": "turn_completed",
            "agent_path": "/root/demo",
            "run_id": "run_demo",
            "status": {"turn_status": "completed", "process_status": "waiting_reply"},
            "payload": {"final_output_ref": "final_output_round_0"},
        }

        event = AgentEvent.from_subagent_event(raw)

        self.assertEqual(event.sequence, 7)
        self.assertEqual(event.event_type, "turn_completed")
        self.assertEqual(event.agent_path, "/root/demo")
        self.assertEqual(event.run_id, "run_demo")
        self.assertEqual(event.status.turn_status, "completed")
        self.assertEqual(event.artifact_ref.artifact_id, "final_output_round_0")


if __name__ == "__main__":
    unittest.main()
