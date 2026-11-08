"""notify_when_idle: be told when an agent frees up, instead of polling for it.

``send_message`` returns as soon as the message is queued; when the target actually
processes it depends on that target's loop. A caller who needs the follow-up has only
polling today, which CC forbids outright ("do NOT sleep, poll, or proactively check").
A one-shot idle subscription fixes that: the idle frame goes out on the notification
stream the fold point already drains, so no new channel and no timer is involved.

The two behaviours that matter and are pinned here: it is one-shot, and it does not
replace the turn-completed notice (a finished turn is not the same as being free).
"""

import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from genericagent.agents.event_bus import SubagentEventBus  # noqa: E402
from genericagent.agents.idle_subscriptions import IdleSubscriptions  # noqa: E402
from genericagent.agents.manager import SubagentManager  # noqa: E402
from genericagent.agents.notifications import peek_subagent_notifications_prompt  # noqa: E402


class IdleSubscriptionsTest(unittest.TestCase):
    def test_subscribe_then_consume_is_one_shot(self):
        with tempfile.TemporaryDirectory() as td:
            subs = IdleSubscriptions(td)

            subs.subscribe("/root/worker", requested_by="/root")
            first = subs.consume("/root/worker")
            second = subs.consume("/root/worker")

            self.assertIsNotNone(first)
            self.assertEqual(first["requested_by"], "/root")
            self.assertIsNone(second, "a subscription must not fire twice")

    def test_resubscribing_after_consumption_works(self):
        with tempfile.TemporaryDirectory() as td:
            subs = IdleSubscriptions(td)
            subs.subscribe("/root/worker")
            subs.consume("/root/worker")

            subs.subscribe("/root/worker")

            self.assertTrue(subs.is_subscribed("/root/worker"))

    def test_consuming_an_unsubscribed_agent_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as td:
            subs = IdleSubscriptions(td)
            self.assertIsNone(subs.consume("/root/nobody"))
            self.assertEqual(subs.list_paths(), [])


class ManagerIdleSubscriptionTest(unittest.TestCase):
    def _manager(self, td):
        class FakeProcess:
            next_pid = 9600

            def __init__(self):
                self.pid = FakeProcess.next_pid
                FakeProcess.next_pid += 1

        return SubagentManager(
            root_dir=td,
            popen=lambda *_, **__: FakeProcess(),
            python_executable="python-test",
            sleep=lambda _: None,
            process_exists=lambda _pid: True,
        )

    def test_send_with_notify_when_idle_records_a_subscription(self):
        with tempfile.TemporaryDirectory() as td:
            manager = self._manager(td)
            manager.spawn_agent("worker", "first")

            manager.send_message("worker", "please redo it", author="/root", notify_when_idle=True)

            subs = IdleSubscriptions(manager.subagents_state_dir)
            self.assertTrue(subs.is_subscribed("/root/worker"))

    def test_send_without_the_flag_records_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            manager = self._manager(td)
            manager.spawn_agent("worker", "first")

            manager.send_message("worker", "no subscription", author="/root")

            self.assertFalse(IdleSubscriptions(manager.subagents_state_dir).is_subscribed("/root/worker"))

    def test_an_idle_frame_reaches_the_notification_stream_once(self):
        with tempfile.TemporaryDirectory() as td:
            manager = self._manager(td)
            manager.spawn_agent("worker", "first")
            manager.send_message("worker", "redo", author="/root", notify_when_idle=True)

            manager.emit_idle_notification("worker", summary="available now")
            manager.emit_idle_notification("worker", summary="available again")

            text, ack_upto = peek_subagent_notifications_prompt(td)
            self.assertIn("available now", text)
            self.assertNotIn("available again", text)
            self.assertTrue(ack_upto)

    def test_an_unsubscribed_idle_frame_does_not_enter_the_notification_stream(self):
        """Turn-completed notices still arrive; idle chatter nobody asked for does not."""
        with tempfile.TemporaryDirectory() as td:
            manager = self._manager(td)
            manager.spawn_agent("worker", "first")

            manager.emit_idle_notification("worker", summary="idle but unsubscribed")

            text, _ = peek_subagent_notifications_prompt(td)
            self.assertNotIn("idle but unsubscribed", text)

    def test_the_idle_frame_is_still_durable_in_the_inbox(self):
        """Losing the push must not lose the information."""
        with tempfile.TemporaryDirectory() as td:
            manager = self._manager(td)
            manager.spawn_agent("worker", "first")

            frame = manager.emit_idle_notification("worker", summary="durable")

            self.assertEqual(frame["type"], "idle_notification")
            inbox = (Path(td) / "temp" / "subagents" / "inbox.jsonl").read_text(encoding="utf-8")
            self.assertIn("durable", inbox)

    def test_a_lead_dm_suppresses_the_idle_frame_entirely(self):
        """CC m1448: do not repeat what the agent already told the lead this turn."""
        with tempfile.TemporaryDirectory() as td:
            manager = self._manager(td)
            manager.spawn_agent("worker", "first")
            manager.send_message("worker", "redo", author="/root", notify_when_idle=True)
            task_dir = Path(td) / "temp" / "worker"
            # A lead DM after the last turn is exactly the suppressing condition.
            with open(task_dir / "events.jsonl", "a", encoding="utf-8") as handle:
                handle.write('{"type": "lead_message"}\n')

            self.assertIsNone(manager.emit_idle_notification("worker", summary="suppressed"))


class IdleNotificationShapeTest(unittest.TestCase):
    def test_the_frame_carries_the_fields_a_caller_needs(self):
        from genericagent.agents.idle import build_idle_notification

        frame = build_idle_notification(
            from_name="worker",
            idle_reason="available",
            summary="done with the batch",
            completed_task_id=7,
            completed_status="completed",
        )

        self.assertEqual(frame["type"], "idle_notification")
        self.assertEqual(frame["from"], "worker")
        self.assertEqual(frame["idleReason"], "available")
        self.assertEqual(frame["completedTaskId"], "7")
        self.assertEqual(frame["completedStatus"], "completed")
        self.assertIn("timestamp", frame)


if __name__ == "__main__":
    unittest.main()
