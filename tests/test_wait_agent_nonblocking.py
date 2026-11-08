"""``wait_agent`` must not hold the turn hostage while it waits.

What was reported: ``wait_agent(target=probe_alpha, condition=process_terminal,
timeout_seconds=180)`` froze the composer for the whole 180s. The user could not tell
whether the app was alive, and nothing they typed could be delivered, because the turn
was parked inside a synchronous ``while True`` + ``sleep`` loop.

Claude Code treats "sleep / poll until the subagent finishes" as an explicit
anti-pattern in its own Agent prompt ("do NOT sleep, poll, or proactively check"). Its
three-layer guarantee is: launch returns immediately, the child is an independent async
task, and the result arrives through a notification that is folded in at the next tool
boundary.

GA already has every piece of that -- event bus, fold-point injection, notification
cursor -- except that ``wait_agent`` stays synchronous. These tests pin the change of
contract:

  * a long ``timeout_seconds`` is capped to a short slice, so the wait returns quickly
    and the turn loop reaches its next fold point (where queued prompts are delivered)
  * the result says "not satisfied yet, call again or do other work", which is what the
    model needs in order to keep making progress instead of blocking
  * ``wait_agent`` still returns *immediately* when the predicate is already satisfied
  * the cap is per-call, not a removal of waiting: callers that ask for a short wait
    keep exactly the wait they asked for
"""

import sys
import time
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from genericagent.agents.manager import SubagentManager  # noqa: E402


class _FakeManager(SubagentManager):
    """Real manager, but every wait observes a still-running child."""

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.slept = []

    def probe_agent(self, target):  # noqa: D102 - test double
        from genericagent.agents.manager import AgentState

        return AgentState(
            task_name=str(target),
            agent_path=f"/root/{target}",
            pid=4242,
            task_dir=str(self.task_root / str(target)),
            turn_status="running",
            process_status="waiting_reply",
            round=0,
            output_path=None,
            final_output_path=None,
        )

    def sleep(self, seconds):
        self.slept.append(seconds)


class WaitAgentIsBoundedTest(unittest.TestCase):
    def _manager(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        return _FakeManager(
            project_root=self._tmp.name,
            project_state_root=self._tmp.name,
            popen=lambda *a, **k: None,
        )

    def test_a_long_timeout_returns_quickly(self):
        """The whole point: 180s must not become 180s of a frozen turn."""
        manager = self._manager()
        started = time.monotonic()

        result = manager.wait_agents(
            targets=["kid"], timeout_s=180, wait_condition="process_terminal"
        )
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 15, "wait_agent still blocks for the requested timeout")
        self.assertFalse(result.satisfied, "an unfinished child reported as satisfied")

    def test_an_unfinished_wait_tells_the_model_what_to_do_next(self):
        manager = self._manager()

        result = manager.wait_agents(
            targets=["kid"], timeout_s=180, wait_condition="process_terminal"
        )

        self.assertTrue(
            result.recommended_next_action,
            "the model got no guidance and will retry the same blocking wait",
        )
        # It must be explicit that waiting again is allowed but not required.
        self.assertRegex(
            result.recommended_next_action,
            "other work|continue|again",
            "guidance does not offer an alternative to blocking again",
        )

    def test_a_short_timeout_is_honoured_as_asked(self):
        """The cap must not turn a deliberate 1s probe into a longer wait."""
        manager = self._manager()

        result = manager.wait_agents(
            targets=["kid"], timeout_s=1, wait_condition="process_terminal"
        )

        self.assertLessEqual(sum(manager.slept), 2.0, "a 1s wait slept longer than asked")
        self.assertFalse(result.satisfied)

    def test_an_already_satisfied_predicate_returns_without_sleeping(self):
        manager = self._manager()

        def finished(target):
            from genericagent.agents.manager import AgentState

            return AgentState(
                task_name=str(target),
                agent_path=f"/root/{target}",
                pid=4242,
                task_dir=str(manager.task_root / str(target)),
                turn_status="completed",
                process_status="shutdown",
                round=0,
                output_path=None,
                final_output_path=None,
            )

        manager.probe_agent = finished

        result = manager.wait_agents(
            targets=["kid"], timeout_s=180, wait_condition="process_terminal"
        )

        self.assertTrue(result.satisfied, "a finished child was not reported as satisfied")
        self.assertEqual([], manager.slept, "it slept even though the child was done")


if __name__ == "__main__":
    unittest.main()
