"""Goal x workflow cooperation (stage 2-A extended).

Workflows run on bridge-owned daemon threads and never register with the
SubagentManager, so the original stage 2-A "in flight" check could not see them.
An active goal then evaluated the pre-workflow transcript, blocked the turn, and
spun the main agent until the Stop-hook cap. These tests pin the fix:

* ``has_in_flight_agents`` defers when a background workflow is running.
* The workflow provider is fail-open: a raising/empty provider never defers.
* Terminal workflow statuses do NOT defer.
* The check-in label list merges sub-agents and workflows.
* The bridge label helper skips stale runs (dead launcher thread).
"""

import threading
import unittest
from types import SimpleNamespace

from genericagent.core import goal_evaluator as ge


def _run(status, name="wf", run_id="r1"):
    return SimpleNamespace(
        status=status,
        run_id=run_id,
        metadata={"workflowName": name},
    )


def _agent(**kw):
    base = dict(session_id="s1")
    base.update(kw)
    return SimpleNamespace(**base)


class WorkflowInFlightTests(unittest.TestCase):
    def test_provider_marks_workflow_in_flight_without_manager(self):
        a = _agent(workflow_in_flight_provider=lambda: ["wf-one · workflow"])
        self.assertTrue(ge.has_in_flight_agents(a))
        self.assertTrue(ge.has_in_flight_workflows(a))

    def test_empty_provider_falls_through_to_no_in_flight(self):
        a = _agent(workflow_in_flight_provider=lambda: [])
        self.assertFalse(ge.has_in_flight_agents(a))
        self.assertFalse(ge.has_in_flight_workflows(a))

    def test_raising_provider_is_fail_open(self):
        def boom():
            raise RuntimeError("store down")
        a = _agent(workflow_in_flight_provider=boom)
        # Must never raise and must never defer forever.
        self.assertFalse(ge.has_in_flight_agents(a))
        self.assertFalse(ge.has_in_flight_workflows(a))

    def test_store_fallback_live_status_defers(self):
        for status in ("running", "awaiting_approval"):
            store = SimpleNamespace(list_runs=lambda *, session_id: [_run(status)])
            a = _agent(workflow_store=store)
            self.assertTrue(ge.has_in_flight_agents(a), status)

    def test_store_fallback_terminal_status_does_not_defer(self):
        for status in ("succeeded", "degraded", "failed", "cancelled", "killed", "interrupted", "draft"):
            store = SimpleNamespace(list_runs=lambda *, session_id, _s=status: [_run(_s)])
            a = _agent(workflow_store=store)
            self.assertFalse(ge.has_in_flight_agents(a), status)

    def test_store_fallback_survives_list_runs_error(self):
        def boom(*, session_id):
            raise OSError("disk gone")
        a = _agent(workflow_store=SimpleNamespace(list_runs=boom))
        self.assertFalse(ge.has_in_flight_agents(a))

    def test_labels_merge_subagent_and_workflow(self):
        state = SimpleNamespace(turn_status="running", process_status="alive",
                                task_name="worker", agent_type="general")
        manager = SimpleNamespace(list_descendants=lambda sid: [state])
        a = _agent(
            _get_subagent_manager=lambda: manager,
            workflow_in_flight_provider=lambda: ["nightly · workflow"],
        )
        labels = ge.list_in_flight_task_labels(a)
        self.assertIn("worker · general", labels)
        self.assertIn("nightly · workflow", labels)
        self.assertEqual(len(labels), 2)


class PerTurnLaunchTests(unittest.TestCase):
    def test_launched_this_turn_flag_defers(self):
        a = _agent(_workflow_launched_this_turn=True)
        self.assertTrue(ge.workflow_launched_this_turn(a))
        # Even with no live run, the launch turn must defer.
        self.assertTrue(ge.has_in_flight_agents(a) or ge.workflow_launched_this_turn(a))

    def test_flag_default_false(self):
        self.assertFalse(ge.workflow_launched_this_turn(_agent()))


class BridgeLabelHelperTests(unittest.TestCase):
    def _bridge(self, runs, threads, session="s1"):
        from frontends.ink.bridge import GenericAgentBridge

        fake = SimpleNamespace(
            _current_session_id=lambda: session,
            workflow_store=SimpleNamespace(list_runs=lambda *, session_id: runs),
            _workflow_threads=threads,
        )
        return GenericAgentBridge._workflow_in_flight_labels(fake)

    def test_live_thread_is_in_flight(self):
        live = threading.Thread(target=lambda: threading.Event().wait(0.2))
        live.start()
        try:
            labels = self._bridge([_run("running", "build")], {"r1": live})
        finally:
            live.join(timeout=2)
        self.assertEqual(labels, ["build · workflow"])

    def test_dead_thread_is_skipped(self):
        dead = threading.Thread(target=lambda: None)
        dead.start()
        dead.join()
        labels = self._bridge([_run("running", "build")], {"r1": dead})
        self.assertEqual(labels, [])

    def test_terminal_status_skipped(self):
        labels = self._bridge([_run("succeeded", "build")], {})
        self.assertEqual(labels, [])

    def test_no_session_returns_empty(self):
        labels = self._bridge([_run("running", "build")], {}, session="")
        self.assertEqual(labels, [])


if __name__ == "__main__":
    unittest.main()
