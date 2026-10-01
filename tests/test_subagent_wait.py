import json
import unittest
import tempfile
from pathlib import Path
from types import SimpleNamespace

from subagent_wait import evaluate_wait_condition
from subagent_manager import SubagentManager
from subagent_state import atomic_write_json


def fake_state(task_name, *, turn_status, process_status, result_status, result_ref=None):
    return SimpleNamespace(
        task_name=task_name,
        turn_status=turn_status,
        process_status=process_status,
        result_status=result_status,
        result_ref=result_ref,
        final_output_path=result_ref,
    )


class WaitConditionTest(unittest.TestCase):
    def test_all_terminal_tracks_remaining_targets(self):
        states = [
            fake_state("a", turn_status="completed", process_status="exited", result_status="available"),
            fake_state("b", turn_status="running", process_status="running", result_status="pending"),
        ]

        decision = evaluate_wait_condition(states, "all_terminal")

        self.assertFalse(decision.satisfied)
        self.assertEqual(decision.remaining_targets, ["b"])
        self.assertEqual(decision.recommended_next_action, "wait_agent")

    def test_result_available_requires_persisted_result_ref(self):
        state = fake_state(
            "a",
            turn_status="completed",
            process_status="exited",
            result_status="pending",
        )

        decision = evaluate_wait_condition([state], "result_available")

        self.assertFalse(decision.satisfied)
        self.assertEqual(decision.recommended_next_action, "wait_agent")

    def test_result_available_returns_result_ref_when_artifact_exists(self):
        state = fake_state(
            "a",
            turn_status="completed",
            process_status="exited",
            result_status="available",
            result_ref="artifacts/a/final_output.json",
        )

        decision = evaluate_wait_condition([state], "result_available")

        self.assertTrue(decision.satisfied)
        self.assertEqual(decision.result_refs, {"a": "artifacts/a/final_output.json"})
        self.assertEqual(decision.recommended_next_action, "read_agent_result")

    def test_manager_wait_all_terminal_does_not_return_on_turn_started(self):
        with tempfile.TemporaryDirectory() as root:
            task_dir = Path(root) / "temp" / "child"
            task_dir.mkdir(parents=True)
            atomic_write_json(
                task_dir / "state.json",
                {
                    "schema_version": 1,
                    "task_name": "child",
                    "agent_path": "/root/child",
                    "pid": None,
                    "round": 0,
                    "turn_status": "running",
                    "process_status": "alive",
                    "output_path": None,
                    "final_output_path": None,
                },
            )
            manager = SubagentManager(root_dir=root, process_exists=lambda pid: True)

            result = manager.wait_agents(targets=["child"], timeout_s=0, wait_condition="all_terminal")

            self.assertTrue(result.timed_out)
            self.assertFalse(result.satisfied)
            self.assertEqual(result.remaining_targets, ["child"])

    def test_read_agent_persists_result_status_and_reference(self):
        with tempfile.TemporaryDirectory() as root:
            task_dir = Path(root) / "temp" / "child"
            task_dir.mkdir(parents=True)
            output_path = task_dir / "output.txt"
            output_path.write_text("done\n\n[ROUND END]\n", encoding="utf-8")
            atomic_write_json(
                task_dir / "state.json",
                {
                    "schema_version": 1,
                    "task_name": "child",
                    "agent_path": "/root/child",
                    "pid": None,
                    "round": 0,
                    "turn_status": "running",
                    "process_status": "alive",
                    "output_path": str(output_path),
                    "final_output_path": None,
                },
            )
            manager = SubagentManager(root_dir=root, process_exists=lambda pid: False)

            state = manager.read_agent("child")
            persisted = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))

            self.assertEqual(state.result_status, "available")
            self.assertEqual(state.result_ref, str(output_path))
            self.assertEqual(persisted["result_status"], "available")
            self.assertEqual(persisted["result_ref"], str(output_path))


if __name__ == "__main__":
    unittest.main()
