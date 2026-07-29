import json
import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from subagent_artifacts import SubagentArtifactStore  # noqa: E402
from subagent_manager import SubagentManager  # noqa: E402
from subagent_state import atomic_write_json  # noqa: E402


class SubagentArtifactStoreTest(unittest.TestCase):
    def test_record_final_output_writes_manifest_with_hash_and_ref(self):
        with tempfile.TemporaryDirectory() as td:
            run_dir = Path(td) / "temp" / "subagents" / "runs" / "run_000001"
            output_path = Path(td) / "temp" / "demo" / "output.txt"
            output_path.parent.mkdir(parents=True)
            output_path.write_text("answer\n", encoding="utf-8")
            store = SubagentArtifactStore(run_dir)

            artifact = store.record_final_output(output_path, round_no=0)

            self.assertEqual(artifact["artifact_id"], "final_output_round_0")
            self.assertEqual(artifact["type"], "final_output")
            self.assertEqual(artifact["bytes"], len(output_path.read_bytes()))
            self.assertEqual(len(artifact["sha256"]), 64)
            manifest = json.loads((run_dir / "artifacts.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["artifacts"], [artifact])

    def test_manager_read_agent_result_prefers_artifact_ref_without_round_end_marker(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td) / "temp" / "demo"
            task_dir.mkdir(parents=True)
            output_path = task_dir / "output.txt"
            output_path.write_text("plain final text without marker", encoding="utf-8")
            manager = SubagentManager(root_dir=td, process_exists=lambda pid: False)
            run_dir = Path(td) / "temp" / "subagents" / "runs" / "run_demo"
            artifact = SubagentArtifactStore(run_dir).record_final_output(output_path, round_no=0)
            atomic_write_json(
                task_dir / "state.json",
                {
                    "schema_version": 1,
                    "task_name": "demo",
                    "agent_path": "/root/demo",
                    "run_id": "run_demo",
                    "artifact_dir": str(run_dir),
                    "pid": None,
                    "round": 0,
                    "turn_status": "running",
                    "process_status": "exited",
                    "output_path": str(output_path),
                    "final_output_path": None,
                    "final_output_ref": artifact["artifact_id"],
                },
            )

            state = manager.read_agent("demo")

            self.assertEqual(state.turn_status, "completed")
            self.assertEqual(Path(state.final_output_path), output_path)


if __name__ == "__main__":
    unittest.main()
