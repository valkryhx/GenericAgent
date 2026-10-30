"""Pure-logic tests for the goal wake scheduler (phases 2-B / 3).

No sleeping and no network: timers are captured by a fake factory and fired by
hand, so every branch of the decision surface is deterministic.
"""
import unittest
from types import SimpleNamespace

from genericagent.core import goal_wake as gw


class _FakeTimer:
    def __init__(self, delay, cb, registry):
        self.delay = delay
        self.cb = cb
        self.cancelled = False
        registry.append(self)
    def cancel(self):
        self.cancelled = True


class _Clock:
    def __init__(self):
        self.timers = []
    def factory(self, delay, cb):
        t = _FakeTimer(delay, cb, self.timers)
        self.last = t
        return t
    def alive(self):
        return [t for t in self.timers if not t.cancelled]


def _agent(condition="finish the report", set_at=100.0, session_id="s1"):
    goal = SimpleNamespace(condition=condition, set_at=set_at)
    return SimpleNamespace(active_goal=goal, session_id=session_id)


class DelayMathTests(unittest.TestCase):
    def test_checkin_backoff_capped(self):
        self.assertEqual(gw.checkin_delay_ms(0, base_minutes=30), 30 * 60000)
        self.assertEqual(gw.checkin_delay_ms(1, base_minutes=30), 60 * 60000)
        self.assertEqual(gw.checkin_delay_ms(2, base_minutes=30), 120 * 60000)
        # beyond the cap exponent stays at 2**2
        self.assertEqual(gw.checkin_delay_ms(5, base_minutes=30), 120 * 60000)

    def test_retry_delays_match_cc(self):
        self.assertEqual(gw.retry_delay_ms(0), 60000)
        self.assertEqual(gw.retry_delay_ms(1), 300000)
        self.assertEqual(gw.retry_delay_ms(2), 900000)
        self.assertEqual(gw.retry_delay_ms(9), 900000)  # clamps to last


class ConfigTests(unittest.TestCase):
    def test_defaults_off(self):
        import os
        for k in ("GA_GOAL_WAKE",):
            os.environ.pop(k, None)
        self.assertFalse(gw.is_enabled())

    def test_enabled_truthy(self):
        import os
        os.environ["GA_GOAL_WAKE"] = "1"
        try:
            self.assertTrue(gw.is_enabled())
        finally:
            os.environ.pop("GA_GOAL_WAKE", None)


class SchedulerTests(unittest.TestCase):
    def _sched(self, agent, wake, tasks=None, on_event=None):
        clock = _Clock()
        s = gw.GoalWakeScheduler(
            agent, wake, timer_factory=clock.factory,
            has_in_flight=(lambda a: list(tasks or [])),
            on_event=on_event,
        )
        return s, clock

    def test_checkin_fires_with_tasks(self):
        sent = []
        agent = _agent()
        s, clock = self._sched(agent, lambda p: sent.append(p) or True, tasks=["t1 · local_bash"])
        self.assertTrue(s.arm_checkin(delay_ms=1000))
        self.assertEqual(len(clock.alive()), 1)
        clock.last.cb()
        self.assertEqual(len(sent), 1)
        self.assertIn("still running", sent[0])
        self.assertIn("t1", sent[0])

    def test_checkin_no_tasks_message(self):
        sent = []
        agent = _agent()
        s, clock = self._sched(agent, lambda p: sent.append(p) or True, tasks=[])
        s.arm_checkin(delay_ms=1000)
        clock.last.cb()
        self.assertIn("no longer running", sent[0])

    def test_stale_goal_dropped(self):
        sent = []
        agent = _agent(set_at=100.0)
        s, clock = self._sched(agent, lambda p: sent.append(p) or True, tasks=["x"])
        s.arm_checkin(delay_ms=1000)
        # goal replaced before it fires
        agent.active_goal = SimpleNamespace(condition="new", set_at=200.0)
        clock.last.cb()
        self.assertEqual(sent, [])  # dropped, no wake

    def test_stale_session_dropped(self):
        sent = []
        agent = _agent(session_id="s1")
        s, clock = self._sched(agent, lambda p: sent.append(p) or True, tasks=["x"])
        s.arm_checkin(delay_ms=1000)
        agent.session_id = "s2"
        clock.last.cb()
        self.assertEqual(sent, [])

    def test_cancel_prevents_fire(self):
        sent = []
        agent = _agent()
        s, clock = self._sched(agent, lambda p: sent.append(p) or True, tasks=["x"])
        s.arm_checkin(delay_ms=1000)
        s.cancel("user_message")
        self.assertFalse(s.pending())
        self.assertTrue(clock.last.cancelled)

    def test_idle_cap_stops_rearming(self):
        sent = []
        agent = _agent()
        events = []
        s, clock = self._sched(agent, lambda p: sent.append(p) or True, tasks=[],
                               on_event=lambda e, o: events.append((e, o)))
        # Fire idle check-ins up to the cap (default 3)
        for _ in range(gw.checkin_idle_cap()):
            s.arm_checkin(delay_ms=1000)
            clock.last.cb()
        self.assertEqual(s.idle_count, gw.checkin_idle_cap())
        self.assertIn("idle check-ins paused", sent[-1])
        self.assertTrue(any(o == "idle_cap_reached" for _, o in events))

    def test_retry_streak_gives_up(self):
        sent = []
        agent = _agent()
        events = []
        s, clock = self._sched(agent, lambda p: sent.append(p) or True,
                               on_event=lambda e, o: events.append((e, o)))
        maxr = gw.retry_max()
        for _ in range(maxr):
            s.arm_retry(cause="provider error", delay_ms=1000)
            if clock.alive():
                clock.last.cb()
        self.assertEqual(s.retry_streak, maxr)
        self.assertTrue(any(o == "gave_up" for _, o in events))
        self.assertIn("Goal paused", sent[-1])

    def test_retry_message_contains_cause(self):
        sent = []
        agent = _agent()
        s, clock = self._sched(agent, lambda p: sent.append(p) or True)
        s.arm_retry(cause="the turn hit the step limit", delay_ms=1000)
        clock.last.cb()
        self.assertIn("step limit", sent[0])

    def test_no_callback_is_noop(self):
        agent = _agent()
        clock = _Clock()
        s = gw.GoalWakeScheduler(agent, None, timer_factory=clock.factory,
                                 has_in_flight=lambda a: ["x"])
        s.arm_checkin(delay_ms=1000)
        clock.last.cb()  # should not raise
        self.assertTrue(True)


if __name__ == "__main__":
    unittest.main()
