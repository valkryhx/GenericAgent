import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from genericagent.agents.event_bus import SubagentEventBus  # noqa: E402
from genericagent.agents.notifications import (  # noqa: E402
    ANTI_SPOOF_PREAMBLE,
    SYSTEM_NOTIFICATION_HEADER,
    build_subagent_notifications_prompt,
    prefix_system_notification,
)


class SubagentNotificationsTest(unittest.TestCase):
    def test_completed_notification_uses_cc_task_notification_format(self):
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
            self.assertIn("[/GA_SUBAGENT_NOTIFICATIONS]", first)
            self.assertIn("<task-notification", first)
            self.assertIn("<task-id>/root/demo</task-id>", first)
            self.assertIn("<status>completed</status>", first)
            self.assertIn("<summary>done</summary>", first)
            self.assertIn("<result-ref>final_output_round_0</result-ref>", first)
            # GA's machine fields survive as attributes.
            self.assertIn('agent-path="/root/demo"', first)
            self.assertIn('run-id="run_demo"', first)
            # the big body is never inlined.
            self.assertNotIn("large body", first)
            self.assertEqual(second, "")

    def test_notifications_carry_the_anti_spoof_preamble(self):
        with tempfile.TemporaryDirectory() as td:
            bus = SubagentEventBus(Path(td) / "temp" / "subagents")
            bus.append_event("turn_completed", agent_path="/root/a", task_name="a", payload={"summary": "ok"}, notify=True)

            prompt = build_subagent_notifications_prompt(Path(td))

            self.assertIn(SYSTEM_NOTIFICATION_HEADER, prompt)
            self.assertIn("NOT a message from the user", prompt)
            self.assertIn("NOT real user input", prompt)
            # the preamble precedes the element so the model reads it first.
            self.assertLess(prompt.index(SYSTEM_NOTIFICATION_HEADER), prompt.index("<task-notification"))

    def test_agent_error_maps_to_failed_and_closed_to_killed(self):
        with tempfile.TemporaryDirectory() as td:
            bus = SubagentEventBus(Path(td) / "temp" / "subagents")
            bus.append_event("agent_error", agent_path="/root/boom", task_name="boom", payload={"error": "x"}, notify=True)
            bus.append_event("agent_closed", agent_path="/root/gone", task_name="gone", payload={"reason": "parent_cleanup"}, notify=True)

            prompt = build_subagent_notifications_prompt(Path(td))

            self.assertIn("<status>failed</status>", prompt)
            self.assertIn("<status>killed</status>", prompt)

    def test_prefix_system_notification_is_idempotent(self):
        once = prefix_system_notification("hello")
        twice = prefix_system_notification(once)

        self.assertTrue(once.startswith(SYSTEM_NOTIFICATION_HEADER))
        self.assertTrue(once.endswith("hello"))
        self.assertEqual(once, twice)


if __name__ == "__main__":
    unittest.main()


class ResultBudgetTest(unittest.TestCase):
    def test_single_block_is_capped_at_the_result_budget(self):
        from genericagent.agents.idle import RESULT_BUDGET
        from genericagent.agents.notifications import apply_result_budget

        out = apply_result_budget(["x" * 5000])
        self.assertTrue(out[0].startswith("x" * RESULT_BUDGET))
        self.assertIn("truncated", out[0])

    def test_whole_drain_is_capped_at_the_drain_budget(self):
        from genericagent.agents.idle import DRAIN_BUDGET, DRAIN_TRUNCATION_HINT
        from genericagent.agents.notifications import apply_result_budget

        out = apply_result_budget(["y" * 5000 for _ in range(5)])
        joined = "".join(out)
        self.assertLessEqual(len(joined), DRAIN_BUDGET + 400)
        self.assertIn(DRAIN_TRUNCATION_HINT, joined)

    def test_under_budget_blocks_are_unchanged(self):
        from genericagent.agents.notifications import apply_result_budget

        self.assertEqual(apply_result_budget(["a", "b"]), ["a", "b"])


if __name__ == "__main__":
    unittest.main()
