import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from subagent_event_bus import SubagentEventBus  # noqa: E402
from subagent_notifications import build_subagent_notifications_prompt  # noqa: E402


class SubagentNotificationsTest(unittest.TestCase):
    def test_completed_notification_prompt_consumes_once_without_final_output_body(self):
        with tempfile.TemporaryDirectory() as td:
            bus = SubagentEventBus(Path(td) / "temp" / "subagents")
            bus.append_event(
                "turn_completed",
                agent_path="/root/demo",
                run_id="run_demo",
                task_name="demo",
                payload={"summary": "done", "final_output_ref": "final_output_round_0", "final_output": "large body"},
                notify=True,
            )

            first = build_subagent_notifications_prompt(Path(td))
            second = build_subagent_notifications_prompt(Path(td))

            self.assertIn("[GA_SUBAGENT_NOTIFICATIONS]", first)
            self.assertIn("<ga_subagent_notification>", first)
            self.assertIn('"agent_path": "/root/demo"', first)
            self.assertIn('"final_output_ref": "final_output_round_0"', first)
            self.assertNotIn("large body", first)
            self.assertEqual(second, "")


if __name__ == "__main__":
    unittest.main()
