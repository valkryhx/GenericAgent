import json
import tempfile
import unittest
from pathlib import Path

from subagent_manager import SubagentManager
from subagent_wait import (
    is_workflow_run_id,
    workflow_run_is_terminal,
    workflow_run_turn_status,
)

RUN_ID = "wf_" + "a" * 32
OTHER_RUN = "wf_" + "b" * 32


def make_manager(root, resolver=None):
    return SubagentManager(root_dir=str(root), sleep=lambda _s: None, workflow_run_resolver=resolver)


class WorkflowRunStatusMappingTest(unittest.TestCase):
    def test_run_id_shape(self):
        self.assertTrue(is_workflow_run_id(RUN_ID))
        self.assertFalse(is_workflow_run_id("research_a"))
        self.assertFalse(is_workflow_run_id("wf_short"))
        self.assertFalse(is_workflow_run_id("/root/wf_" + "z" * 32))

    def test_degraded_is_terminal_but_reports_completed_turn(self):
        self.assertTrue(workflow_run_is_terminal("degraded"))
        self.assertEqual("completed", workflow_run_turn_status("degraded"))
        self.assertEqual("completed", workflow_run_turn_status("succeeded"))
        self.assertEqual("failed", workflow_run_turn_status("failed"))
        self.assertEqual("killed", workflow_run_turn_status("killed"))
        self.assertEqual("running", workflow_run_turn_status("running"))
        self.assertFalse(workflow_run_is_terminal("running"))


class WorkflowWaitTerminalTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _resolver(self, status, result_ref=None):
        def resolve(run_id):
            if run_id != RUN_ID:
                return None
            return {
                "status": status,
                "artifactDir": str(self.root / "artifacts" / run_id),
                "resultRef": result_ref,
                "updatedAt": "2026-10-13T00:00:00",
            }
        return resolve

    def test_running_run_does_not_satisfy_and_does_not_hang_forever(self):
        manager = make_manager(self.root, self._resolver("running"))
        result = manager.wait_agents(targets=[RUN_ID], timeout_s=0.05, poll_interval_s=0.01, wait_condition="workflow_terminal")
        self.assertFalse(result.satisfied)
        self.assertTrue(result.timed_out)
        self.assertEqual([RUN_ID], result.remaining_targets)

    def test_terminal_run_satisfies_immediately(self):
        manager = make_manager(self.root, self._resolver("succeeded", result_ref="/tmp/final-result.json"))
        result = manager.wait_agents(targets=[RUN_ID], timeout_s=5, wait_condition="workflow_terminal")
        self.assertTrue(result.satisfied)
        self.assertFalse(result.timed_out)
        self.assertEqual([RUN_ID], result.terminal_targets)
        self.assertEqual("/tmp/final-result.json", result.result_refs.get(RUN_ID))
        self.assertEqual("read_agent_result", result.recommended_next_action)

    def test_degraded_run_is_terminal(self):
        manager = make_manager(self.root, self._resolver("degraded"))
        result = manager.wait_agents(targets=[RUN_ID], timeout_s=5, wait_condition="workflow_terminal")
        self.assertTrue(result.satisfied)
        self.assertEqual([RUN_ID], result.terminal_targets)

    def test_run_transitioning_to_terminal_satisfies_within_timeout(self):
        calls = {"n": 0}

        def resolve(run_id):
            calls["n"] += 1
            return {"status": "running" if calls["n"] < 3 else "succeeded", "artifactDir": str(self.root)}

        manager = make_manager(self.root, resolve)
        result = manager.wait_agents(targets=[RUN_ID], timeout_s=2, poll_interval_s=0.01, wait_condition="workflow_terminal")
        self.assertTrue(result.satisfied)
        self.assertGreaterEqual(calls["n"], 3)

    def test_workflow_target_never_creates_orphan_subagent_state(self):
        manager = make_manager(self.root, self._resolver("running"))
        manager.probe_agent(RUN_ID)
        manager.read_agent(RUN_ID)
        self.assertFalse((self.root / "temp" / RUN_ID / "state.json").exists())

    def test_unknown_run_id_resolves_to_none(self):
        manager = make_manager(self.root, self._resolver("succeeded"))
        self.assertIsNone(manager.resolve_workflow_run(OTHER_RUN))

    def test_default_resolver_reads_durable_run_state(self):
        manager = make_manager(self.root, None)
        run_dir = self.root / "temp" / "sessions" / "sess1" / "workflows" / RUN_ID
        run_dir.mkdir(parents=True)
        (run_dir / "state.json").write_text(json.dumps({"status": "degraded"}), encoding="utf-8", newline=chr(10))
        (run_dir / "final-result.json").write_text("{}", encoding="utf-8", newline=chr(10))
        state = manager.resolve_workflow_run(RUN_ID)
        self.assertIsNotNone(state)
        self.assertEqual("completed", state.turn_status)
        self.assertEqual("degraded", state.completion_status)
        self.assertEqual("available", state.result_status)


class UnknownWorkflowTargetFastFailTest(unittest.TestCase):
    def test_unknown_workflow_targets_lists_only_unresolved_runs(self):
        from ga import GenericAgentHandler

        class _Manager:
            def resolve_workflow_run(self, run_id):
                return {"status": "running"} if run_id == RUN_ID else None

        unknown = GenericAgentHandler._unknown_workflow_targets([RUN_ID, OTHER_RUN, "research_a"], _Manager())
        self.assertEqual([OTHER_RUN], unknown)


class _Thread:
    def __init__(self, alive):
        self._alive = alive

    def is_alive(self):
        return self._alive


class WorkflowStallGuardTest(unittest.TestCase):
    """A launcher thread that died without finalizing must not block the wait."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def _bridge(self, alive):
        from frontends.ink.bridge import GenericAgentBridge
        from workflow_models import WorkflowRun
        from workflow_store import WorkflowStore

        store = WorkflowStore(root=self.root)
        run = WorkflowRun(session_id="sess", script="// x", run_id=RUN_ID, status="running")
        store.create_run(run)
        bridge = GenericAgentBridge.__new__(GenericAgentBridge)
        bridge.workflow_store = store
        bridge._workflow_threads = {RUN_ID: _Thread(alive)}
        return bridge

    def test_dead_launcher_reports_stale(self):
        info = self._bridge(alive=False)._resolve_workflow_run_info(RUN_ID)
        self.assertEqual("stale", info["status"])

    def test_live_launcher_stays_running(self):
        info = self._bridge(alive=True)._resolve_workflow_run_info(RUN_ID)
        self.assertEqual("running", info["status"])


if __name__ == "__main__":
    unittest.main()
