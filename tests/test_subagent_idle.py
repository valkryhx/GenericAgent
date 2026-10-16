import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from genericagent.agents.idle import (  # noqa: E402
    DRAIN_BUDGET,
    RESULT_BUDGET,
    build_idle_notification,
    sent_lead_dm_since_last_turn,
    truncate_drain,
    truncate_result,
)


class ResultBudgetTest(unittest.TestCase):
    def test_short_result_is_untouched(self):
        self.assertEqual(truncate_result("hello"), "hello")

    def test_long_result_is_truncated_with_a_hint(self):
        text = "x" * (RESULT_BUDGET + 500)

        out = truncate_result(text)

        self.assertLessEqual(len(out), RESULT_BUDGET + 100)
        self.assertIn("truncated", out)

    def test_drain_budget_truncates_the_overflowing_frame(self):
        parts = ["a" * 9000, "b" * 9000]

        out = truncate_drain(parts)

        self.assertEqual(out[0], "a" * 9000)
        self.assertIn("truncated - ask the agent for the rest via send_message]", out[1])
        self.assertTrue(out[1].startswith("b" * 7000))
        self.assertLessEqual(sum(len(item) for item in out[:1]), DRAIN_BUDGET)


class IdleNotificationTest(unittest.TestCase):
    def test_frame_carries_the_cc_fields(self):
        frame = build_idle_notification(
            from_name="agent_1",
            idle_reason="available",
            summary="all done\nwith two lines",
            completed_task_id="3",
            completed_status="completed",
            result="r" * 10,
        )

        self.assertEqual(frame["type"], "idle_notification")
        self.assertEqual(frame["from"], "agent_1")
        self.assertEqual(frame["idleReason"], "available")
        self.assertEqual(frame["summary"], "all done with two lines")
        self.assertEqual(frame["completedTaskId"], "3")
        self.assertEqual(frame["completedStatus"], "completed")
        self.assertEqual(frame["result"], "r" * 10)

    def test_optional_fields_are_omitted_when_absent(self):
        frame = build_idle_notification(from_name="a", idle_reason="available")

        self.assertNotIn("completedTaskId", frame)
        self.assertNotIn("result", frame)


class LeadDmDedupTest(unittest.TestCase):
    def _write(self, td, rows):
        path = Path(td) / "events.jsonl"
        path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
        return path

    def test_lead_dm_after_the_last_turn_suppresses_the_idle_result(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write(td, [{"type": "turn_completed"}, {"type": "lead_message"}])

            self.assertTrue(sent_lead_dm_since_last_turn(path))

    def test_lead_dm_before_the_last_turn_does_not_suppress(self):
        with tempfile.TemporaryDirectory() as td:
            path = self._write(td, [{"type": "lead_message"}, {"type": "turn_completed"}])

            self.assertFalse(sent_lead_dm_since_last_turn(path))

    def test_missing_events_file_is_not_a_dm(self):
        self.assertFalse(sent_lead_dm_since_last_turn(Path("/nonexistent/events.jsonl")))


if __name__ == "__main__":
    unittest.main()
