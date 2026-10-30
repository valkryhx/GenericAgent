"""Wiring tests for goal auto-wake (phases 2-B / 3).

Uses a fake scheduler so no real timers/threads/network are involved. Confirms
`_maybe_arm_goal_wake_after_turn` arms the right thing per turn outcome and
that goal lifecycle points cancel a pending wake.
"""
import os
import unittest
from types import SimpleNamespace

from genericagent.core.app import GenericAgent
from genericagent.core import goal_wake as gw


class _FakeSched:
    def __init__(self):
        self.checkins = 0
        self.retries = []
        self.cancels = []
        self.cause = None
    def arm_checkin(self, delay_ms=None):
        self.checkins += 1
        return True
    def arm_retry(self, cause="", delay_ms=None):
        self.retries.append(cause)
        self.cause = cause
        return True
    def cancel(self, reason=""):
        self.cancels.append(reason)
    def reset_streaks(self):
        pass
    def pending(self):
        return True


def _agent(**kw):
    a = GenericAgent.__new__(GenericAgent)
    a._goal_wake = None
    a._goal_wake_callback = lambda p: True
    a.active_goal = SimpleNamespace(condition="finish", set_at=1.0)
    a.is_subagent = False
    a.session_id = "s"
    a.stop_sig = False
    a._goal_data_root = lambda: None
    for k, v in kw.items():
        setattr(a, k, v)
    return a


class _mkenv:
    def __init__(self, val="1"):
        self.val = val
    def __enter__(self):
        self.old = os.environ.get("GA_GOAL_WAKE")
        if self.val is None:
            os.environ.pop("GA_GOAL_WAKE", None)
        else:
            os.environ["GA_GOAL_WAKE"] = self.val
    def __exit__(self, *a):
        if self.old is None:
            os.environ.pop("GA_GOAL_WAKE", None)
        else:
            os.environ["GA_GOAL_WAKE"] = self.old


class ArmDecisionTests(unittest.TestCase):
    def test_off_by_default_does_nothing(self):
        with _mkenv(None):
            a = _agent()
            fs = _FakeSched()
            a._goal_wake = fs
            a._maybe_arm_goal_wake_after_turn(SimpleNamespace(last_goal_defer=False),
                                              {"kind": "max_turns"})
            self.assertEqual(fs.retries, [])
            self.assertEqual(fs.checkins, 0)

    def test_no_callback_does_nothing(self):
        with _mkenv("1"):
            a = _agent(_goal_wake_callback=None)
            a._goal_wake = _FakeSched()
            a._maybe_arm_goal_wake_after_turn(SimpleNamespace(last_goal_defer=False),
                                              {"kind": "max_turns"})
            self.assertEqual(a._goal_wake.retries, [])

    def test_no_goal_does_nothing(self):
        with _mkenv("1"):
            a = _agent(active_goal=None)
            a._goal_wake = _FakeSched()
            a._maybe_arm_goal_wake_after_turn(SimpleNamespace(last_goal_defer=False),
                                              {"kind": "max_turns"})
            self.assertEqual(a._goal_wake.retries, [])

    def test_user_abort_never_rearms(self):
        with _mkenv("1"):
            a = _agent()
            a._goal_wake = _FakeSched()
            a._maybe_arm_goal_wake_after_turn(SimpleNamespace(last_goal_defer=False),
                                              {"kind": "user_abort"})
            self.assertEqual(a._goal_wake.retries, [])

    def test_max_turns_arms_retry(self):
        with _mkenv("1"):
            a = _agent()
            a._goal_wake = _FakeSched()
            a._maybe_arm_goal_wake_after_turn(
                SimpleNamespace(last_goal_defer=False),
                {"kind": "max_turns", "cause": "hit step limit"})
            self.assertEqual(a._goal_wake.retries, ["hit step limit"])

    def test_error_arms_retry(self):
        with _mkenv("1"):
            a = _agent()
            a._goal_wake = _FakeSched()
            a._maybe_arm_goal_wake_after_turn(
                SimpleNamespace(last_goal_defer=False),
                {"kind": "error", "cause": "provider error"})
            self.assertEqual(a._goal_wake.retries, ["provider error"])

    def test_defer_arms_checkin_even_on_completed(self):
        with _mkenv("1"):
            a = _agent()
            a._goal_wake = _FakeSched()
            a._maybe_arm_goal_wake_after_turn(
                SimpleNamespace(last_goal_defer=True),
                {"kind": "completed"})
            self.assertEqual(a._goal_wake.checkins, 1)
            self.assertEqual(a._goal_wake.retries, [])

    def test_completed_not_deferred_does_nothing(self):
        with _mkenv("1"):
            a = _agent()
            a._goal_wake = _FakeSched()
            a._maybe_arm_goal_wake_after_turn(
                SimpleNamespace(last_goal_defer=False), {"kind": "completed"})
            self.assertEqual(a._goal_wake.checkins, 0)
            self.assertEqual(a._goal_wake.retries, [])


class CancelTests(unittest.TestCase):
    def test_cancel_on_new_user_message(self):
        a = _agent()
        a._goal_wake = _FakeSched()
        a._cancel_goal_wake("user_message")
        self.assertEqual(a._goal_wake.cancels, ["user_message"])


if __name__ == "__main__":
    unittest.main()
