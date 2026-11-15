"""How much wall time does ONE iteration of `wait_agents` actually cost?

What it measures
----------------
`WaitAgentsWatchTest.test_a_broken_channel_falls_back_to_sleeping_instead_of_failing_the_wait`
calls `wait_agents(timeout_s=0.05, poll_interval_s=0.02)` and asserts that the
manager degraded to a blind sleep (`self.assertTrue(slept, ...)`).

That assertion requires the loop to complete **at least one full iteration** —
probe targets, read the event bus, hit the broken channel, fall back, sleep — inside
a 0.05 s budget. If one iteration costs more than 50 ms on a busy machine, the
deadline expires first and `slept` stays empty, so the test fails while the behaviour
under test (degrade to polling) was never reached.

This probe measures the per-iteration cost directly, with no test framework in the
way, so the number can be compared against the 50 ms budget instead of guessed at.

Root cause
----------
The budget is expressed as wall-clock seconds, while the thing it must cover is a
fixed amount of *work* (filesystem probes + event bus reads). Wall time is the work
divided by available CPU, so a loaded machine inflates the same work past the budget.
That is why the failure needs load to show up and why it looks like a flake.

Why it must be kept
-------------------
"Passed 5/5 under 6 CPU hogs" is not evidence of anything (CLAUDE.md rule 2) — the
first attempt at reproducing this failed exactly that way. The per-iteration cost is
the durable number: it says how much headroom a 0.05 s budget really has, and it is
what any future budget should be sized against.

Usage
-----
    python tests/probes/python/_probe_wait_agents_iteration_cost.py
    python tests/probes/python/_probe_wait_agents_iteration_cost.py --rounds 20

Exit code is always 0: this is a measurement. Read the verdict line.
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

TEST_BUDGET_S = 0.05   # what the test passes as timeout_s
POLL_INTERVAL_S = 0.02  # what the test passes as poll_interval_s


class _BrokenChannel:
    """Mirrors the stub in the test: touching it raises, forcing the fallback."""

    def wait_for_signal(self, timeout):
        raise OSError("channel is gone")


def _build_manager(td: str):
    """A manager with one registered child, matching the test's fixture shape."""
    from genericagent.agents.manager import SubagentManager

    task_dir = Path(td) / "temp" / "broken"
    task_dir.mkdir(parents=True)
    (task_dir / "output.txt").write_text("broken working", encoding="utf-8")
    state = {
        "schema_version": 1,
        "task_name": "broken",
        "agent_path": "/root/broken",
        "pid": 500,
        "round": 0,
        "turn_status": "running",
        "process_status": "alive",
        "status": "running",
        "task_dir": str(task_dir),
        "input_path": str(task_dir / "input.txt"),
        "output_path": str(task_dir / "output.txt"),
        "started_at": "2026-01-01T00:00:00",
        "updated_at": "2026-01-01T00:00:00",
    }
    (task_dir / "state.json").write_text(__import__("json").dumps(state), encoding="utf-8")

    slept: list[float] = []
    manager = SubagentManager(root_dir=td, process_exists=lambda _pid: True, sleep=slept.append)
    manager._realtime_channels["/root/broken"] = _BrokenChannel()
    return manager, slept


def measure_rounds(rounds: int) -> tuple[list[float], int]:
    """Run the test body once per round; return (per-round elapsed, slept-count)."""
    elapsed: list[float] = []
    total_sleeps = 0
    for _ in range(rounds):
        with tempfile.TemporaryDirectory() as td:
            manager, slept = _build_manager(td)
            started = time.monotonic()
            manager.wait_agents(["broken"], timeout_s=TEST_BUDGET_S,
                                poll_interval_s=POLL_INTERVAL_S)
            elapsed.append(time.monotonic() - started)
            total_sleeps += len(slept)
    return elapsed, total_sleeps


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Per-iteration cost of wait_agents.")
    parser.add_argument("--rounds", type=int, default=20)
    args = parser.parse_args(argv[1:])

    print(f"repo       : {REPO}")
    print(f"test budget: {TEST_BUDGET_S * 1000:.0f} ms   poll interval: {POLL_INTERVAL_S * 1000:.0f} ms")
    print(f"rounds     : {args.rounds}")

    elapsed, sleeps = measure_rounds(args.rounds)
    ms = [e * 1000 for e in elapsed]
    observable = sum(1 for e in elapsed if e >= TEST_BUDGET_S)
    print()
    print(f"  wait_agents() took: min={min(ms):.1f}ms  median={statistics.median(ms):.1f}ms  "
          f"max={max(ms):.1f}ms")
    print(f"  rounds that consumed the whole budget: {observable}/{len(ms)}")
    print(f"  total blind sleeps observed         : {sleeps}")
    print()
    print("How to read this")
    print("  The assertion needs at least one full loop iteration (probe targets, read")
    print("  the bus, fail the channel, fall back, sleep) to fit inside the budget.")
    print("  `slept` staying empty means the deadline fired first — the degrade-to-")
    print("  polling behaviour the test claims to check never ran.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
