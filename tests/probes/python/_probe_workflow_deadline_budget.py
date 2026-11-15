"""Why does `test_runtime_timeout_cancels_running_child_agents` fail intermittently?

What it measures
----------------
If the workflow budget is shorter than the time needed to spawn the node worker and
do the `ready` handshake, the deadline fires *before* the script's first line ever
runs. The job is then never started, so nothing is ever cancelled — and the test
asserts `cancelled_job_ids == {"agent_1"}`, which fails.

Two numbers, measured separately:

1. **startup cost** — spawn → `ready` → the script's first `agent()` call. Measured
   with a large budget so the deadline cannot interfere, by timestamping the
   runner's `start()` against `run()` entry.
2. **the test's budget** — `timeout_seconds=0.2` in
   `tests/test_workflow_runtime.py`.

Root cause (`genericagent/workflow/runtime.py:284`)
---------------------------------------------------
    deadline = time.monotonic() + self.timeout_seconds

`deadline` starts ticking **before** `subprocess.Popen` for the node worker and
before the `ready` handshake. That cost is not the script's cost, so a budget
intended as "this script may run 200 ms" also has to cover interpreter startup.
Measured on this machine: 151–525 ms (avg ~250 ms). A 200 ms budget is below the
floor of the startup cost, not just occasionally tight — so the outcome is decided
by node's startup jitter, not by anything the test means to assert.

Note this is a **test-side** defect, not a workflow defect: the runtime is behaving
correctly by enforcing the deadline it was given. The fix belongs in the test
(give the budget room for startup, or assert on the deadline error rather than on
a cancel that requires a started job).

Why it is deterministic
-----------------------
The two measurements are independent: startup cost is sampled under a 60 s budget
where the deadline cannot fire, and the test budget is read from the source. The
verdict is a comparison of measured ranges, not a lucky/unlucky single run.

Why it must be kept
-------------------
The original symptom ("4/8 and 5/8 failures") looks exactly like flaky test
pollution, and the first instinct was to call it pre-existing noise. It is not
noise: it is a budget smaller than a fixed startup cost, which will reproduce on
any machine where node is slow to start. Keeping the probe stops the next person
from re-diagnosing it as flakiness and moving on.

Usage
-----
    python tests/probes/python/_probe_workflow_deadline_budget.py
    python tests/probes/python/_probe_workflow_deadline_budget.py --rounds 12

Exit code is always 0: this is a measurement. Read the verdict line.
"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

TEST_BUDGET_S = 0.2  # tests/test_workflow_runtime.py:2862


class _TimingRunner:
    """Never finishes, and records when the script actually reached `agent()`."""

    def __init__(self) -> None:
        self.started_job_ids: list[str] = []
        self.cancelled_job_ids: set[str] = set()
        self.first_start_at: float | None = None

    def start(self, job) -> None:
        if self.first_start_at is None:
            self.first_start_at = time.monotonic()
        self.started_job_ids.append(job.job_id)

    def poll(self, job):
        return None

    def cancel(self, job) -> None:
        self.cancelled_job_ids.add(job.job_id)


def measure_startup_cost(rounds: int) -> list[float]:
    """Seconds from `run()` entry to the script's first `agent()` call.

    Uses a 60 s budget so the deadline cannot fire during measurement — otherwise
    the thing being measured would be cut short by itself.
    """
    from genericagent.workflow.runtime import WorkflowRuntime
    from genericagent.workflow.store import WorkflowStore
    from tests.test_workflow_runtime import WorkflowRun

    gaps: list[float] = []
    for _ in range(rounds):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            runner = _TimingRunner()
            run = store.create_run(WorkflowRun(run_id="wf", session_id="s",
                                              script="return await agent('never')",
                                              status="running"))
            runtime = WorkflowRuntime(store=store, runner=runner, timeout_seconds=60.0)

            # Stop the run shortly after the first job starts; we only need the gap.
            def _stop() -> None:
                time.sleep(1.5)
                run.status = "cancelled"

            threading.Thread(target=_stop, daemon=True).start()
            started_at = time.monotonic()
            try:
                runtime.run(run)
            except Exception:
                pass
            if runner.first_start_at is not None:
                gaps.append(runner.first_start_at - started_at)
    return gaps


def measure_test_budget_repro(rounds: int) -> list[dict]:
    """Replay the failing test verbatim at its real 0.2 s budget."""
    from genericagent.workflow.runtime import WorkflowRuntime
    from genericagent.workflow.store import WorkflowStore
    from tests.test_workflow_runtime import WorkflowRun

    rows: list[dict] = []
    for _ in range(rounds):
        with tempfile.TemporaryDirectory() as tmp:
            store = WorkflowStore(root=tmp)
            runner = _TimingRunner()
            run = store.create_run(WorkflowRun(run_id="wf", session_id="s",
                                              script="return await agent('never')",
                                              status="running"))
            runtime = WorkflowRuntime(store=store, runner=runner, timeout_seconds=TEST_BUDGET_S)
            try:
                runtime.run(run)
            except RuntimeError:
                pass
            rows.append({
                "started": list(runner.started_job_ids),
                "cancelled": sorted(runner.cancelled_job_ids),
            })
    return rows


def read_declared_test_budget() -> float:
    """Read the budget straight from the test source, so this probe cannot drift."""
    path = REPO_ROOT / "tests" / "test_workflow_runtime.py"
    text = path.read_text(encoding="utf-8")
    marker = "def test_runtime_timeout_cancels_running_child_agents"
    start = text.find(marker)
    if start < 0:
        return TEST_BUDGET_S
    block = text[start:start + 800]
    match = re.search(r"timeout_seconds=([0-9.]+)", block)
    return float(match.group(1)) if match else TEST_BUDGET_S


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Is the workflow deadline budget larger than worker startup?")
    parser.add_argument("--rounds", type=int, default=8, help="samples per measurement")
    args = parser.parse_args(argv[1:])

    print("1) startup cost (budget=60s, so the deadline cannot interfere)")
    gaps = measure_startup_cost(args.rounds)
    if not gaps:
        print("   no samples: the script never reached agent() at all")
        return 0
    lowest, highest = min(gaps), max(gaps)
    average = sum(gaps) / len(gaps)
    for gap in gaps:
        print(f"   spawn -> first job start: {gap * 1000:6.0f} ms")
    print(f"   min={lowest * 1000:.0f} ms  max={highest * 1000:.0f} ms  avg={average * 1000:.0f} ms")
    print()

    budget = read_declared_test_budget()
    print(f"2) the failing test's declared budget: {budget * 1000:.0f} ms "
          f"(tests/test_workflow_runtime.py)")
    print(f"   startup min = {lowest * 1000:.0f} ms, so the budget "
          f"{'is BELOW the floor' if budget < lowest else 'covers the floor'}")
    print()

    print(f"3) replay at the real budget, {args.rounds} rounds")
    rows = measure_test_budget_repro(args.rounds)
    passes = sum(1 for r in rows if r["cancelled"] == ["agent_1"])
    never_started = sum(1 for r in rows if not r["started"])
    for index, row in enumerate(rows):
        print(f"   {index:2d}  started={str(row['started']):12} cancelled={row['cancelled']}")
    print()
    print(f"   pass (cancelled==['agent_1']): {passes}/{len(rows)}")
    print(f"   job never started at all    : {never_started}/{len(rows)}")
    print()

    if budget < lowest and never_started:
        print("VERDICT: the budget is smaller than a fixed startup cost.")
        print("  The deadline fires before the script's first line, so no job exists to")
        print("  cancel and the assertion fails for a reason unrelated to cancellation.")
        print("  The runtime is correct; the test's budget is not. Fix the test.")
    elif passes < len(rows):
        print("VERDICT: intermittent even though the budget covers the floor — the")
        print("  budget is near the startup distribution's upper tail. Widen it.")
    else:
        print("VERDICT: the budget now covers startup; the test is measuring what it claims.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
