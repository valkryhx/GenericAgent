"""Opt-in real gpt-5.6-luna E2E for the P0 workflow gates.

Run with ``GA_RUN_REAL_P0_WORKFLOW_E2E=1 python tests/real_workflow_p0_e2e.py``.
The child is allowed to edit only a temporary workspace. The summary contains
statuses, gate metadata, and tool names; it never prints prompts or transcripts.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from sensitive_redaction import redact_sensitive_text, sanitize
from workflow_child_agent import NativeGPTChildAgentRunner
from workflow_llm import binding_from_profile
from workflow_models import WorkflowRun
from workflow_runtime import WorkflowRuntime
from workflow_scheduler import SchedulerConfig
from workflow_store import WorkflowStore


PROFILE = "luna"
EXPECTED_MODEL = "gpt-5.6-luna"
OPT_IN = os.environ.get("GA_RUN_REAL_P0_WORKFLOW_E2E") == "1"

WORKFLOW_SCRIPT = r"""
phase('Green Gate and Repair')
const repair = await repairAndRetest({
  workspacePath: args.workspacePath,
  pattern: 'test_*.py',
  timeoutMs: 30000,
  maxAttempts: 1,
  labelPrefix: 'repair',
  repairPrompt: `Read ${args.workspacePath}/TEST_FAILURES.txt, ${args.workspacePath}/calculator.py, and ${args.workspacePath}/test_calculator.py. Use file_read and file_write to make the smallest repair so add(2, 3) returns 5. Do not read mykey.py, mykey.json, mcp.json, or credentials. Do not write outside ${args.workspacePath}.`
})
return {
  passed: repair.passed,
  gatePassed: repair.gatePassed,
  repairAttempts: repair.repairAttempts,
  gateKey: repair.gateKey
}
"""


def _transcript_tool_names(run, job) -> list[str]:
    if not job.metadata.get("transcriptRef"):
        return []
    path = Path(run.artifact_dir) / job.metadata["transcriptRef"]
    if not path.exists():
        return []
    names: set[str] = set()
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("type") == "tool_call" and event.get("toolName"):
            names.add(str(event["toolName"]))
    return sorted(names)


def _gate_summaries(run) -> list[dict]:
    gate_dir = Path(run.artifact_dir) / "test-gates"
    rows = []
    for path in sorted(gate_dir.glob("gate-*.json")) if gate_dir.exists() else []:
        data = json.loads(path.read_text(encoding="utf-8"))
        rows.append(
            {
                "gateId": data.get("gateId"),
                "gateKey": data.get("gateKey"),
                "expectation": data.get("expectation"),
                "passed": data.get("passed"),
                "gatePassed": data.get("gatePassed"),
                "timedOut": data.get("timedOut"),
                "artifactRef": data.get("artifactRef"),
                "failureRef": data.get("failureRef"),
                "workspaceFailureRef": data.get("workspaceFailureRef"),
            }
        )
    return rows


def main() -> int:
    summary = {
        "passed": False,
        "skipped": not OPT_IN,
        "profile": PROFILE,
        "expectedModel": EXPECTED_MODEL,
        "issues": [],
    }
    if not OPT_IN:
        summary["reason"] = "set GA_RUN_REAL_P0_WORKFLOW_E2E=1 to run the real gpt-5.6-luna P0 E2E"
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    root = Path(tempfile.mkdtemp(prefix="ga_real_p0_workflow_"))
    workspace = root / "workspace"
    workspace.mkdir(parents=True)
    (workspace / "calculator.py").write_text(
        "def add(left, right):\n"
        "    return left - right\n",
        encoding="utf-8",
    )
    (workspace / "test_calculator.py").write_text(
        "import unittest\n\n"
        "from calculator import add\n\n"
        "class CalculatorTest(unittest.TestCase):\n"
        "    def test_add(self):\n"
        "        self.assertEqual(add(2, 3), 5)\n",
        encoding="utf-8",
    )

    store = WorkflowStore(root=root / "runtime")
    run = store.create_run(
        WorkflowRun(
            run_id="wf_real_p0_luna",
            session_id="real_p0_luna",
            script=WORKFLOW_SCRIPT,
            status="running",
        )
    )
    try:
        binding = binding_from_profile(PROFILE)
        summary["binding"] = binding.as_metadata()
        if binding.model_id != EXPECTED_MODEL:
            summary["issues"].append("profile_model_mismatch")
            raise RuntimeError(f"profile {PROFILE} did not resolve to expected model")

        runner = NativeGPTChildAgentRunner(
            profile_name=PROFILE,
            enable_tools=True,
            max_tokens=768,
            max_turns=12,
        )
        outcome = WorkflowRuntime(
            store=store,
            runner=runner,
            scheduler_config=SchedulerConfig(max_concurrent=1, max_total=2),
            timeout_seconds=300.0,
        ).run(run, args={"workspacePath": str(workspace)})
        loaded = store.load_run(run.run_id)
        gates = _gate_summaries(loaded)
        jobs = [
            {
                "jobId": job.job_id,
                "label": job.metadata.get("label"),
                "status": job.status,
                "tools": _transcript_tool_names(loaded, job),
            }
            for job in loaded.jobs
        ]
        summary.update(
            {
                "runStatus": loaded.status,
                "result": sanitize(outcome.result),
                "gates": gates,
                "jobs": jobs,
                "workspaceFailureLog": (workspace / "TEST_FAILURES.txt").exists(),
                "fixedSource": "return left + right" in (workspace / "calculator.py").read_text(encoding="utf-8"),
                "artifactDir": str(loaded.artifact_dir),
            }
        )
        if loaded.status != "succeeded":
            summary["issues"].append("workflow_not_succeeded")
        if not outcome.result.get("gatePassed"):
            summary["issues"].append("final_gate_not_passed")
        if outcome.result.get("repairAttempts") != 1:
            summary["issues"].append("repair_attempt_count_mismatch")
        if len(gates) != 2 or not gates[0]["passed"] is False or not gates[1]["gatePassed"]:
            summary["issues"].append("unexpected_gate_sequence")
        if not summary["workspaceFailureLog"] or not summary["fixedSource"]:
            summary["issues"].append("repair_did_not_use_workspace_contract")
        if [job["label"] for job in jobs] != ["repair-1"] or jobs[0]["status"] != "succeeded":
            summary["issues"].append("repair_job_contract_failed")
        summary["passed"] = not summary["issues"]
    except Exception as exc:
        summary["error"] = redact_sensitive_text(str(exc))
        try:
            loaded = store.load_run(run.run_id)
            summary["runStatus"] = loaded.status
            summary["gates"] = _gate_summaries(loaded)
            summary["jobStatuses"] = [job.status for job in loaded.jobs]
            summary["artifactDir"] = str(loaded.artifact_dir)
        except Exception:
            pass

    print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
    return 0 if summary["passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
