"""Turn-outcome classification for goal auto-retry (phase 2-B/3 precondition).

`app.run()` used to do `for chunk in gen`, which discards the generator's
`return {"result": ...}`. Phase 3 must tell completed / max_turns / user abort /
error apart, so `_drain_agent_stream` captures the return value and normalizes
it via `_classify_turn_outcome`.
"""
import unittest
from unittest import mock

from genericagent.core.app import GenericAgent


def _bare_agent(**kw):
    a = GenericAgent.__new__(GenericAgent)
    a.stop_sig = False
    a.inc_out = True
    a.task_dir = None
    for k, v in kw.items():
        setattr(a, k, v)
    return a


class ClassifyTurnOutcomeTests(unittest.TestCase):
    def test_current_task_done_is_completed(self):
        out = _bare_agent()._classify_turn_outcome({"result": "CURRENT_TASK_DONE"})
        self.assertEqual(out["kind"], "completed")
        self.assertEqual(out["exit_reason"], "CURRENT_TASK_DONE")

    def test_max_turns(self):
        out = _bare_agent()._classify_turn_outcome({"result": "MAX_TURNS_EXCEEDED"})
        self.assertEqual(out["kind"], "max_turns")

    def test_exited(self):
        out = _bare_agent()._classify_turn_outcome({"result": "EXITED"})
        self.assertEqual(out["kind"], "exited")

    def test_user_abort_wins_over_result(self):
        # A user stop sets stop_sig; even if the loop returned done, we must not
        # auto-retry against the user's intent.
        out = _bare_agent(stop_sig=True)._classify_turn_outcome({"result": "CURRENT_TASK_DONE"})
        self.assertEqual(out["kind"], "user_abort")

    def test_none_result_is_unknown(self):
        out = _bare_agent()._classify_turn_outcome(None)
        self.assertEqual(out["kind"], "unknown")
        self.assertIsNone(out["exit_reason"])

    def test_unknown_result_string_is_completed(self):
        out = _bare_agent()._classify_turn_outcome({"result": "SOMETHING_ELSE"})
        self.assertEqual(out["kind"], "completed")
        self.assertEqual(out["exit_reason"], "SOMETHING_ELSE")


class _DQ:
    def __init__(self):
        self.items = []
    def put(self, item):
        self.items.append(item)


class DrainAgentStreamTests(unittest.TestCase):
    def test_captures_generator_return_value(self):
        def gen():
            yield "hello"
            yield " world"
            return {"result": "MAX_TURNS_EXCEEDED"}

        a = _bare_agent()
        dq = _DQ()
        with mock.patch("genericagent.core.app.should_flush_display_delta", lambda *args: False), \
             mock.patch("genericagent.core.app.consume_file", lambda *a, **k: False):
            full, outcome = a._drain_agent_stream(gen(), dq, "user")
        self.assertEqual(full, "hello world")
        self.assertEqual(outcome["kind"], "max_turns")
        # tail flush still emitted because inc_out + buffered text
        self.assertTrue(any("next" in it for it in dq.items))

    def test_stop_sig_breaks_and_marks_user_abort(self):
        def gen():
            yield "partial"
            yield "never"

        a = _bare_agent()
        # abort() flips stop_sig; simulate it happening during consumption
        a.abort = lambda: setattr(a, "stop_sig", True)
        dq = _DQ()
        with mock.patch("genericagent.core.app.should_flush_display_delta", lambda *args: False), \
             mock.patch("genericagent.core.app.consume_file", lambda *a, **k: True):
            full, outcome = a._drain_agent_stream(gen(), dq, "user")
        # consume_file True -> abort() -> stop_sig -> break before adding chunk
        self.assertEqual(outcome["kind"], "user_abort")
        self.assertEqual(full, "")

    def test_completed_outcome(self):
        def gen():
            yield "ok"
            return {"result": "CURRENT_TASK_DONE"}

        a = _bare_agent()
        dq = _DQ()
        with mock.patch("genericagent.core.app.should_flush_display_delta", lambda *args: False), \
             mock.patch("genericagent.core.app.consume_file", lambda *a, **k: False):
            full, outcome = a._drain_agent_stream(gen(), dq, "user")
        self.assertEqual(outcome["kind"], "completed")
        self.assertEqual(full, "ok")


if __name__ == "__main__":
    unittest.main()
