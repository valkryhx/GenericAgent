"""Opt-in serial real-model check for terminal wait predicates."""
from __future__ import annotations

import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))
OPT_IN = os.environ.get("GA_RUN_REAL_E2E") == "1"
MODEL = os.environ.get("GA_REAL_API_EXPECTED_MODEL", "deepseek-v4.1-flash")
PROFILE = os.environ.get("GA_REAL_API_EXPECTED_NAME", "deepseek-v4.1-flash")


def _profile():
    from llm_client import load_clients_from_yaml
    sink = io.StringIO()
    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        clients, *_ = load_clients_from_yaml(start_dir=REPO)
    for index, client in enumerate(clients):
        backend = getattr(client, "backend", None)
        name, model = getattr(backend, "name", ""), getattr(backend, "model", "")
        if model == MODEL and (not PROFILE or PROFILE in {"*", name} or PROFILE in name):
            return index, {"name": name, "model": model}
    raise RuntimeError(f"profile not found: {PROFILE}/{MODEL}")


def _events(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip()]


def _child_metrics(task_dir):
    events = _events(task_dir / "events.jsonl")
    startup = _events(task_dir / "startup.jsonl")
    timestamps = {}
    for row in events:
        if row.get("type") and row.get("ts"):
            timestamps.setdefault(row["type"], row["ts"])
    startup_elapsed = {row.get("phase"): row.get("elapsed_ms") for row in startup if row.get("phase")}
    return {
        "eventSequence": [row.get("type") for row in events],
        "startupPhases": startup_elapsed,
        "turnStartedAt": timestamps.get("turn_started"),
        "turnCompletedAt": timestamps.get("turn_completed"),
        "outputMtime": (task_dir / "output.txt").stat().st_mtime if (task_dir / "output.txt").exists() else None,
    }


@unittest.skipUnless(OPT_IN, "set GA_RUN_REAL_E2E=1 for live DeepSeek test")
class RealSubagentWaitTerminalE2E(unittest.TestCase):
    def test_serial_children_wait_for_terminal_and_expose_results(self):
        from subagent_manager import SubagentManager
        llm_no, profile = _profile()
        manager = SubagentManager(root_dir=REPO, python_executable=sys.executable)
        suffix = f"{os.getpid()}_{time.time_ns()}"
        names, report_dir = [], Path(tempfile.mkdtemp(prefix="ga_wait_e2e_"))
        report = {"profile": profile, "waitPredicates": [], "waitReturnCount": 0, "duplicateSpawnCount": 0}
        started = time.monotonic()
        try:
            first = manager.spawn_agent(f"wait_a_{suffix}", "读取 workflow_planner.py，列出两个边界条件。只读，不调用工具。回答包含 WAIT_A_DONE。", llm_no=llm_no, parent_session_id=suffix, permission_profile="read_only", fork_turns="none")
            names.append(first.task_name)
            early = manager.wait_agents(names, timeout_s=0, wait_condition="all_terminal")
            report["waitReturnCount"] += 1
            report["waitPredicates"].append("all_terminal")
            self.assertFalse(early.satisfied, "spawn/turn_started cannot satisfy all_terminal")
            self.assertIn(first.task_name, early.remaining_targets)
            done_a = manager.wait_agents(names, timeout_s=900, wait_condition="turn_terminal")
            report["waitReturnCount"] += 1
            report["waitPredicates"].append("turn_terminal")
            self.assertTrue(done_a.satisfied, done_a.message)
            state_a = manager.probe_agent(first.task_name)
            self.assertEqual("completed", state_a.turn_status)
            self.assertIn("WAIT_A_DONE", Path(state_a.final_output_path).read_text(encoding="utf-8", errors="replace"))

            second = manager.spawn_agent(f"wait_b_{suffix}", "读取 workflow_planner.py，列出两个不同边界条件。只读，不调用工具。回答包含 WAIT_B_DONE。", llm_no=llm_no, parent_session_id=suffix, permission_profile="read_only", fork_turns="none")
            names.append(second.task_name)
            all_done = manager.wait_agents(names, timeout_s=900, wait_condition="all_terminal")
            report["waitReturnCount"] += 1
            report["waitPredicates"].append("all_terminal")
            self.assertTrue(all_done.satisfied, all_done.message)
            self.assertEqual(set(names), set(all_done.result_refs))
            self.assertEqual("read_agent_result", all_done.recommended_next_action)
            for handle, marker in ((first, "WAIT_A_DONE"), (second, "WAIT_B_DONE")):
                child_metrics = _child_metrics(Path(handle.task_dir))
                report.setdefault("children", {})[handle.task_name] = child_metrics
                self.assertIn("turn_started", child_metrics["eventSequence"])
                self.assertIn("turn_completed", child_metrics["eventSequence"])
                state = manager.probe_agent(handle.task_name)
                self.assertIn(marker, Path(state.final_output_path).read_text(encoding="utf-8", errors="replace"))
            report["workflowTotalMs"] = round((time.monotonic() - started) * 1000, 2)
            report["resultRefs"] = all_done.result_refs
            report["passed"] = True
            (report_dir / "metrics.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            print(json.dumps({"passed": True, "metrics": report}, ensure_ascii=False))
        finally:
            for name in names:
                with contextlib.suppress(Exception):
                    manager.close_agent(name, reason="wait_terminal_e2e_done", grace_s=1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
