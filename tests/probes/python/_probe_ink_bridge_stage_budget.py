"""Does the ink bridge's workflow stage budget actually cover worker startup?

What it measures
----------------
The elapsed time from `workflow_approve()` returning to the moment the workflow's
**second** `agent()` call starts. That single number is what the 3.0 s ceiling in
`test_workflow_resume_cache_hit_replays_prefix_without_restarting_cached_child` had to
cover, and it is measured directly instead of being inferred from whether a wait
timed out.

Root cause
----------
`WorkflowRuntime` starts its deadline before the node worker exists
(`genericagent/workflow/runtime.py:284`), so every budget in that test also pays for
process spawn + the `ready` handshake — not just the two `agent()` calls. Observed
directly under 6x CPU load: **2.97 s** against a **3.0 s** ceiling. A 30 ms margin
means the verdict came from scheduler noise, not from the resume-from-cache behaviour
the test asserts.

Why the verdict is arithmetic, not a wait() result
--------------------------------------------------
The first version of this probe judged "would 3.0 s have covered it?" by running
`event.wait(timeout=3.0)` and checking the return value. That is invalid here: the
workflow thread spends its time in CPU-heavy node startup and IPC, so the waiting
thread is starved off the GIL and `wait(timeout=3.0)` can return **True after 4.5 s**
— it overshoots the timeout and then observes an event that was already set. The
probe therefore reported "passed" for a stage that took 4.5 s against a 3.0 s budget.

So this version records timestamps and compares them:

    stage_seconds = (seconds until agent_2 starts) - (seconds at approve)
    covered_by(budget) = stage_seconds <= budget

No thread-wait semantics are involved, so the answer cannot be corrupted by GIL
starvation, and a `--delay` injection gives a real control group:

    --delay 0        -> stage is fast; the old 3.0 s budget usually covers it
    --delay 4        -> stage is provably > 3.0 s; the old budget cannot cover it,
                        the new 15 s one always does

Run it from the repository root: the workflow workspace defaults to the project data
bucket, and a cwd with no GA history resolves to a directory that does not exist yet.

Why it must be kept
-------------------
It is the reproducible record of *why* the constant is 15 s, and it encodes the trap
that cost a full debugging round: `Event.wait(timeout=N)` is not a reliable
stopwatch when other threads are CPU-bound.

Usage
-----
    python tests/probes/python/_probe_ink_bridge_stage_budget.py
    python tests/probes/python/_probe_ink_bridge_stage_budget.py --delay 4 --rounds 3

Exit code is always 0: this is a measurement. Read the verdict lines.
"""

from __future__ import annotations

import argparse
import statistics
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
for _path in (REPO, REPO / "tests"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

SCRIPT = """
const first = await agent('prefix succeeds')
const second = await agent('stop while this is running')
return { marker: 'GA_STOP_RESUME_DONE', first: first.summary, second: second.summary }
"""

OLD_BUDGET = 3.0    # what the test used to allow
NEW_BUDGET = 15.0   # WORKFLOW_STAGE_BUDGET_SECONDS after the fix


class TimestampRunner:
    """Records when agent_2 starts, without relying on Event.wait's timing."""

    def __init__(self, delay_seconds: float = 0.0):
        import threading

        self.started_job_ids: list[str] = []
        self.cancelled_job_ids: list[str] = []
        self.first_completed = False
        self.second_started = threading.Event()
        self.second_cancelled_once = False
        self.delay_seconds = float(delay_seconds)
        self.second_started_at: float | None = None

    def start(self, job) -> None:
        if job.job_id == "agent_2" and self.delay_seconds:
            time.sleep(self.delay_seconds)
        if job.job_id == "agent_2":
            self.second_started_at = time.monotonic()
            self.second_started.set()
        self.started_job_ids.append(job.job_id)

    def poll(self, job):
        from genericagent.workflow.child_agent import AgentResult

        if job.job_id == "agent_1" and not self.first_completed:
            self.first_completed = True
            return AgentResult(
                job_id=job.job_id,
                status="succeeded",
                payload={"summary": "prefix cached by resume"},
                transcript_events=[{"type": "assistant", "text": "prefix transcript"}],
            )
        if job.job_id == "agent_2" and self.second_cancelled_once:
            return AgentResult(
                job_id=job.job_id,
                status="succeeded",
                payload={"summary": "fresh after stop"},
                transcript_events=[{"type": "assistant", "text": "fresh transcript"}],
            )
        return None

    def cancel(self, job) -> None:
        self.cancelled_job_ids.append(job.job_id)
        self.second_cancelled_once = True


def measure_stage(delay_seconds: float, budget: float) -> float | None:
    """Seconds from approve() returning to agent_2 starting. None if it never started."""
    import tests.test_ink_bridge as T
    from genericagent.workflow.runtime import WorkflowRuntime
    from genericagent.workflow.scheduler import SchedulerConfig

    agent = T.FakeAgent()
    agent.session_id = "session_probe"
    events: list = []
    runner = TimestampRunner(delay_seconds)

    def runtime_factory(*, store, timeout_seconds=10.0):
        return WorkflowRuntime(
            store=store,
            runner=runner,
            scheduler_config=SchedulerConfig(max_concurrent=1, max_total=4),
            timeout_seconds=timeout_seconds,
        )

    with tempfile.TemporaryDirectory() as tmp:
        bridge = T.make_isolated_bridge(
            agent_factory=lambda: agent,
            emit=events.append,
            workflow_root=tmp,
            workflow_runtime_factory=runtime_factory,
        )
        run_id = bridge.workflow_draft(T._cc_script(SCRIPT))
        if not bridge.workflow_approve(run_id, timeout_seconds=budget):
            return None
        approved_at = time.monotonic()

        # Bounded by the budget; this loop only decides when to stop *observing*.
        # The verdict never comes from this wait's return value.
        runner.second_started.wait(timeout=budget + 5.0)

        stage = None if runner.second_started_at is None else runner.second_started_at - approved_at
        bridge.workflow_stop(run_id, reason="probe stop")
        bridge.wait_for_workflow_idle(run_id, timeout=budget)
        return stage


def run_condition(label: str, delay: float, rounds: int) -> list[float]:
    print(f"\n{label}  (injected start delay={delay}s)")
    stages: list[float] = []
    for index in range(rounds):
        stage = measure_stage(delay, NEW_BUDGET)
        if stage is None:
            print(f"  round {index}: agent_2 never started")
            continue
        stages.append(stage)
        mark = "covers" if stage <= OLD_BUDGET else "EXCEEDS the old 3.0s"
        print(f"  round {index}: stage={stage:.2f}s  -> {mark}")
    if stages:
        print(f"  min={min(stages):.2f}s  max={max(stages):.2f}s  "
              f"median={statistics.median(stages):.2f}s")
    return stages


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Measure the ink bridge workflow stage duration.")
    parser.add_argument("--rounds", type=int, default=5, help="rounds per condition")
    parser.add_argument("--delay", type=float, default=None,
                        help="injected start delay for the control condition (default: 4.0)")
    args = parser.parse_args(argv[1:])

    control_delay = 4.0 if args.delay is None else args.delay

    print(f"repo          : {REPO}")
    print(f"old budget    : {OLD_BUDGET}s")
    print(f"new budget    : {NEW_BUDGET}s")
    print(f"rounds        : {args.rounds} per condition")

    natural = run_condition("A. NATURAL (no injected delay)", 0.0, args.rounds)
    control = run_condition("B. CONTROL (slow stage made explicit)", control_delay, args.rounds)

    print()
    print("VERDICT")
    problems: list[str] = []
    if not control:
        problems.append("the control produced no measurements — nothing to conclude")
    elif all(stage > OLD_BUDGET for stage in control):
        print(f"  ✔ a {control_delay}s stage provably exceeds the old {OLD_BUDGET}s ceiling.")
        if natural and max(natural) <= OLD_BUDGET:
            print(f"  ✔ and the natural stage ({max(natural):.2f}s) fits it — so the old")
            print("    budget was not simply wrong, it was too close: the failure needed")
            print("    the natural stage to drift past 3.0s, which is what a loaded")
            print("    machine does. Widening the ceiling removes that cliff entirely.")
        elif natural:
            print(f"  ✖ but the natural stage already reached {max(natural):.2f}s, which")
            print(f"    exceeds the old {OLD_BUDGET}s ceiling in normal conditions too.")
    else:
        problems.append(
            f"the control stage did not exceed {OLD_BUDGET}s — raise --delay above it"
        )

    if problems:
        for problem in problems:
            print(f"  ✖ {problem}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
