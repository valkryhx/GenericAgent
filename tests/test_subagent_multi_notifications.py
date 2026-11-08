"""Several subagents finishing at once: every notice must arrive, none may merge away.

This is the multi-agent case, and it is the one the single-child tests cannot cover.
The bus is *shared*: three children append to one ``notifications.jsonl``, and the main
agent drains it with one cursor. The two ways that goes wrong are:

  * last-writer-wins -- a naive "save the latest notification" implementation would keep
    only one of the three;
  * cursor overshoot -- acking to ``notifications[-1].event_seq`` after rendering only
    part of the batch would silently drop the rest for good.

Both fail silently: the main agent simply never learns that two of its children finished,
and the turn continues as if nothing happened. That is why these tests assert on the
rendered text of every child rather than on counts.

The wait_agent contract changed to non-blocking (``manager.WAIT_SLICE_MAX_S``), which
makes this bus the *only* way the main agent learns a child finished. That is what makes
the "no notice may be lost" guarantee load-bearing rather than merely nice to have.
"""

import sys
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from genericagent.agents.event_bus import SubagentEventBus  # noqa: E402
from genericagent.agents.notifications import (  # noqa: E402
    ack_subagent_notifications,
    peek_subagent_notifications_prompt,
)
from genericagent.agents.manager import SubagentManager  # noqa: E402


def _child_finished(bus, name, *, status="completed", summary=None):
    """Append the event a real child appends when its turn ends."""
    return bus.append_event(
        "turn_completed",
        agent_path=f"/root/{name}",
        run_id=f"run_{name}",
        task_name=name,
        status={"turn_status": status, "process_status": "waiting_reply"},
        payload={"summary": summary or f"{name} finished"},
        notify=True,
    )


class ThreeChildrenTest(unittest.TestCase):
    """Three children, one bus, one drain: all three must be visible."""

    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.manager = SubagentManager(
            project_root=self._tmp.name,
            project_state_root=self._tmp.name,
            project_data_dir=Path(self._tmp.name) / "projects" / "k",
            parent_session_id="s1",
        )
        self.bus = SubagentEventBus(self.manager.subagents_state_dir)

    def _peek(self):
        return peek_subagent_notifications_prompt(
            self._tmp.name,
            data_root=self._tmp.name,
            project_data_dir=str(Path(self._tmp.name) / "projects" / "k"),
            session_id="s1",
        )

    def test_every_child_appears_in_one_drain(self):
        for name in ("alpha", "bravo", "charlie"):
            _child_finished(self.bus, name)

        text, ack = self._peek()

        for name in ("alpha", "bravo", "charlie"):
            self.assertIn(
                f"/root/{name}", text,
                f"{name}'s completion notice was lost when three arrived together",
            )
        self.assertGreater(ack or 0, 0)

    def test_three_notifications_render_as_three_blocks(self):
        for name in ("alpha", "bravo", "charlie"):
            _child_finished(self.bus, name)

        text, _ack = self._peek()

        self.assertEqual(
            3, text.count("<task-notification"),
            "notifications were merged into fewer blocks than children that finished",
        )
        self.assertEqual(3, text.count("</task-notification>"))

    def test_acking_after_the_drain_clears_all_three(self):
        for name in ("alpha", "bravo", "charlie"):
            _child_finished(self.bus, name)

        text, ack = self._peek()
        ack_subagent_notifications(
            self._tmp.name,
            ack,
            data_root=self._tmp.name,
            project_data_dir=str(Path(self._tmp.name) / "projects" / "k"),
            session_id="s1",
        )
        again, ack2 = self._peek()

        self.assertEqual("", again, "acked notices came back")
        self.assertIsNone(ack2)

    def test_a_child_that_finishes_after_the_drain_still_arrives(self):
        """The cursor must not overshoot: a later child is not swallowed by an earlier ack."""
        _child_finished(self.bus, "alpha")
        _text1, ack = self._peek()
        ack_subagent_notifications(
            self._tmp.name,
            ack,
            data_root=self._tmp.name,
            project_data_dir=str(Path(self._tmp.name) / "projects" / "k"),
            session_id="s1",
        )

        _child_finished(self.bus, "bravo")
        text2, _ = self._peek()

        self.assertIn("/root/bravo", text2)
        self.assertNotIn("/root/alpha", text2, "an already-delivered notice came back")

    def test_a_staggered_batch_keeps_the_arrival_order(self):
        """alpha finished first, so the model should read it first."""
        for name in ("alpha", "bravo", "charlie"):
            _child_finished(self.bus, name)

        text, _ack = self._peek()

        self.assertLess(text.index("/root/alpha"), text.index("/root/bravo"))
        self.assertLess(text.index("/root/bravo"), text.index("/root/charlie"))

    def test_a_failing_child_and_a_succeeding_one_are_both_reported(self):
        """A batch is not homogeneous: statuses must not collapse into one word."""
        _child_finished(self.bus, "alpha", status="completed")
        self.bus.append_event(
            "agent_error",
            agent_path="/root/bravo",
            run_id="run_bravo",
            task_name="bravo",
            status={"turn_status": "errored", "process_status": "exited"},
            payload={"error": "boom"},
            notify=True,
        )

        text, _ack = self._peek()

        self.assertIn("/root/alpha", text)
        self.assertIn("/root/bravo", text)
        # The two blocks must not share a single status word.
        self.assertIn("<status>completed</status>", text)
        self.assertIn("<status>failed</status>", text)

    def test_the_bus_is_the_only_path_so_a_dropped_notice_is_unrecoverable(self):
        """Guards the assumption the non-blocking wait_agent now depends on.

        ``notify`` is what puts an event on the notification stream; without it the event
        exists only in ``events.jsonl``, which the main agent never drains. If a future
        change flipped this flag off for ``turn_completed``, children would finish and the
        main agent would simply never hear about it -- so assert it explicitly.
        """
        event = _child_finished(self.bus, "alpha")
        self.assertTrue(event.get("event_seq"))

        notifications = self.bus.peek_notifications()
        self.assertEqual(
            ["turn_completed"],
            [item.get("type") for item in notifications],
            "turn_completed is not on the notification stream any more",
        )


if __name__ == "__main__":
    unittest.main()
