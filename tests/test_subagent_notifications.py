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
    ack_subagent_notifications,
    build_subagent_notifications_prompt,
    peek_subagent_notifications_prompt,
    prefix_system_notification,
)


class NotificationPeekAckTest(unittest.TestCase):
    """Peek must be replayable; only an explicit ack may advance the cursor.

    The old read-and-advance contract lost notices whenever rendering succeeded but
    delivery did not (the model call raised, the turn was aborted). Duplicating a
    notice costs one redundant model call; dropping one means the main agent never
    learns the task finished.
    """

    def _bus(self, td):
        return SubagentEventBus(Path(td) / "temp" / "subagents")

    def _emit(self, bus, summary, seq_agent="a"):
        return bus.append_event(
            "turn_completed",
            agent_path=f"/root/{seq_agent}",
            task_name=seq_agent,
            payload={"summary": summary},
            notify=True,
        )

    def test_peek_does_not_advance_the_cursor(self):
        with tempfile.TemporaryDirectory() as td:
            bus = self._bus(td)
            self._emit(bus, "one")
            self._emit(bus, "two")

            first = bus.peek_notifications()
            second = bus.peek_notifications()

            self.assertEqual(len(first), 2)
            self.assertEqual([e["event_seq"] for e in first], [e["event_seq"] for e in second])

    def test_ack_moves_the_cursor_and_only_forward(self):
        with tempfile.TemporaryDirectory() as td:
            bus = self._bus(td)
            first = self._emit(bus, "one")
            self._emit(bus, "two")

            bus.ack_notifications(first["event_seq"])
            remaining = bus.peek_notifications()
            self.assertEqual(len(remaining), 1)
            self.assertEqual(remaining[0]["payload"]["summary"], "two")

            # A late ack from an older batch must not rewind the cursor.
            bus.ack_notifications(first["event_seq"])
            self.assertEqual(len(bus.peek_notifications()), 1)

            bus.ack_notifications(remaining[0]["event_seq"])
            self.assertEqual(bus.peek_notifications(), [])

    def test_consume_notifications_still_reads_and_advances(self):
        """Backward compatibility: existing callers keep the drain semantics."""
        with tempfile.TemporaryDirectory() as td:
            bus = self._bus(td)
            self._emit(bus, "one")

            self.assertEqual(len(bus.consume_notifications()), 1)
            self.assertEqual(bus.consume_notifications(), [])

    def test_peek_prompt_returns_the_ack_sequence_for_the_rendered_batch(self):
        with tempfile.TemporaryDirectory() as td:
            bus = self._bus(td)
            self._emit(bus, "one")
            last = self._emit(bus, "two")

            text, ack_upto = peek_subagent_notifications_prompt(Path(td))

            self.assertIn("<summary>two</summary>", text)
            self.assertEqual(ack_upto, last["event_seq"])
            # Nothing was acked yet, so the same batch comes back.
            again, ack_again = peek_subagent_notifications_prompt(Path(td))
            self.assertEqual(again, text)
            self.assertEqual(ack_again, ack_upto)

    def test_ack_function_clears_after_delivery(self):
        with tempfile.TemporaryDirectory() as td:
            bus = self._bus(td)
            self._emit(bus, "one")
            text, ack_upto = peek_subagent_notifications_prompt(Path(td))

            ack_subagent_notifications(Path(td), ack_upto)

            self.assertEqual(peek_subagent_notifications_prompt(Path(td)), ("", None))

    def test_empty_peek_reports_no_ack(self):
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(peek_subagent_notifications_prompt(Path(td)), ("", None))


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
