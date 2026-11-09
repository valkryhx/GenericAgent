"""Scope guard for the Ink bridge's workflow cursor seeding.

``GenericAgentBridge._initial_agent_cursors()`` used to call the unscoped
``WorkflowStore.list_runs()``. ``temp/sessions`` is shared by every GA session,
so each Ink start loaded and replayed every historical workflow run (~1s with
118 runs, growing without bound). See
``docs/202609...-GA-workflow启动卡顿与journal性能分析.md``.
"""
import tempfile
import unittest
from pathlib import Path


from genericagent.core.models import make_workflow_source_cursor
from genericagent.workflow.models import WorkflowEvent, WorkflowRun


def _new_store(td):
    from genericagent.workflow.store import WorkflowStore

    return WorkflowStore(Path(td))


def _run_with_events(store, session_id, event_count, run_id):
    run = store.create_run(WorkflowRun(session_id=session_id, script="return 1;", run_id=run_id))
    for index in range(event_count):
        store.append_event(
            run,
            WorkflowEvent(run_id=run.run_id, session_id=session_id, event_type="agent_log", sequence=0, payload={"i": index}),
        )
    return run


class WorkflowAdapterInitialCursorsTest(unittest.TestCase):
    def _adapter(self, store, session_id):
        from genericagent.core.control.workflow import WorkflowChildAdapter

        return WorkflowChildAdapter(store, None, session_id_provider=lambda: session_id)

    def test_it_seeds_only_the_current_sessions_runs(self):
        with tempfile.TemporaryDirectory() as td:
            store = _new_store(td)
            mine = _run_with_events(store, "session_a", 2, "wf_1794000000-aaaa")
            _run_with_events(store, "session_b", 5, "wf_1794000000-bbbb")

            cursors = self._adapter(store, "session_a").initial_cursors()

            self.assertEqual({make_workflow_source_cursor(mine.run_id): 2}, cursors)

    def test_it_does_not_scan_anything_when_the_session_is_unknown(self):
        with tempfile.TemporaryDirectory() as td:
            store = _new_store(td)
            _run_with_events(store, "session_a", 1, "wf_1794000000-cccc")
            calls = []
            original = store.list_runs

            def guarded_list_runs(*, session_id=None):
                calls.append(session_id)
                # An unscoped scan is exactly the bug this guard exists for.
                self.assertIsNotNone(session_id, "initial_cursors must never scan every session")
                return original(session_id=session_id)

            store.list_runs = guarded_list_runs

            self.assertEqual({}, self._adapter(store, None).initial_cursors())
            self.assertEqual([], calls)


class BridgeInitialCursorsTest(unittest.TestCase):
    def test_workflow_cursors_come_from_the_adapter_not_an_unscoped_store_scan(self):
        from frontends.ink.bridge import GenericAgentBridge

        class Bus:
            def last_event_seq(self):
                return 41

        class ProcessAdapter:
            engine = "process"
            source_cursor = "process"
            manager = type("Manager", (), {"event_bus": Bus()})()

        class WorkflowAdapter:
            engine = "workflow"

            class Store:
                def list_runs(self, **kwargs):
                    raise AssertionError("bridge must not enumerate workflow runs itself")

            store = Store()

            def __init__(self):
                self.calls = 0

            def initial_cursors(self):
                self.calls += 1
                return {"workflow:wf_x": 7}

        workflow_adapter = WorkflowAdapter()
        control = type("Control", (), {"adapters": (ProcessAdapter(), workflow_adapter)})()
        bridge = GenericAgentBridge.__new__(GenericAgentBridge)
        bridge.agent_control = control

        cursors = bridge._initial_agent_cursors()

        self.assertEqual(1, workflow_adapter.calls)
        self.assertEqual({"process": 41, "workflow:wf_x": 7}, cursors)


if __name__ == "__main__":
    unittest.main()
