"""Reproduce the two "flaky under full-suite" failures deterministically.

Why not CPU hogs
----------------
Six CPU-bound threads did **not** reproduce either failure (5/5 green each). That is
not a reason to call them flakes: it means the load model was wrong. Both tests fail
on *wall-clock* budgets covering a fixed amount of *work*, and the work here is
dominated by filesystem stats and event-bus reads, not by raw arithmetic. A CPU hog
inflates arithmetic; what inflates filesystem work is contention — many processes
touching the same directories, which is exactly what a full-suite run looks like and
what a single-process hog does not.

So this probe reproduces the real condition by injecting latency into the helpers the
loop calls on every iteration, making one iteration provably more expensive than the
budget. That is the deterministic control: it must fail, every round. Without such a
control the natural measurement only says "green on an idle box", which is exactly the
observation that was misread as "cannot reproduce".

Root cause (both tests, same shape)
-----------------------------------
* `test_a_broken_channel_falls_back_to_sleeping_instead_of_failing_the_wait` —
  `wait_agents(timeout_s=0.05, poll_interval_s=0.02)`. The deadline is
  `now + 0.05`; the loop must complete probe → bus read → broken-channel fallback →
  sleep inside it. When one iteration costs more than 50 ms, the deadline expires
  before the fallback, `slept` stays empty, and the assertion blames the fallback
  for never running.
* `test_abort_stops_mcp_discovery_before_first_model_request` —
  `discovery_started.wait(timeout=2)` plus a 0.75 s window for `abort()` to flip
  `is_running`. Both are wall-clock budgets over a thread handshake.

Why it must be kept
-------------------
It records that "6 CPU hogs, 5/5 green" was a **failed** reproduction attempt rather
than evidence of absence, and it encodes which load model actually maps to the
failure. Without it the next person repeats the hog experiment and concludes "can't
reproduce, must be fine".

Usage
-----
    python tests/probes/python/_probe_wait_budget_wallclock.py
    python tests/probes/python/_probe_wait_budget_wallclock.py --delay 0.03 --rounds 5

Exit code is always 0: this is a measurement. Read the verdict lines.
"""

from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
for _path in (REPO, REPO / "tests"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

BUDGET_S = 0.05
POLL_S = 0.02


class _BrokenChannel:
    """Raises like the test's stub, and records that it was reached at all.

    `calls` is what separates the two failure modes: empty means the loop's budget
    expired before it ever touched the channel, so the fallback was never exercised
    and the test's assertion blames the wrong thing.
    """

    calls: list[float] = []

    def wait_for_signal(self, timeout):
        _BrokenChannel.calls.append(timeout)
        raise OSError("channel is gone")


def _build(td: str):
    from genericagent.agents.manager import SubagentManager

    task_dir = Path(td) / "temp" / "broken"
    task_dir.mkdir(parents=True)
    (task_dir / "output.txt").write_text("broken working", encoding="utf-8")
    (task_dir / "state.json").write_text(json.dumps({
        "schema_version": 1, "task_name": "broken", "agent_path": "/root/broken",
        "pid": 500, "round": 0, "turn_status": "running",
        "process_status": "alive", "status": "running",
        "task_dir": str(task_dir), "input_path": str(task_dir / "input.txt"),
        "output_path": str(task_dir / "output.txt"),
        "started_at": "2026-01-01T00:00:00", "updated_at": "2026-01-01T00:00:00",
    }), encoding="utf-8")

    slept: list[float] = []
    _BrokenChannel.calls = []
    manager = SubagentManager(root_dir=td, process_exists=lambda _pid: True,
                              sleep=slept.append)
    manager._realtime_channels["/root/broken"] = _BrokenChannel()
    return manager, slept, _BrokenChannel.calls


def round_once(delay: float, budget: float = BUDGET_S) -> tuple[bool, bool, float]:
    """One wait_agents call. Returns (channel_reached, slept_at_least_once, elapsed).

    `delay` is injected into the per-iteration helpers the loop actually calls,
    making one loop iteration provably more expensive than `budget`.

    Returning `channel_reached` separately is the point: it distinguishes "the dead
    channel broke the wait" (channel reached, sleep skipped) from "the budget expired
    before the loop got there" (channel never reached). The old test could only report
    the first, which is why a tight budget looked like a broken fallback.
    """
    with tempfile.TemporaryDirectory() as td:
        manager, slept, channel_calls = _build(td)
        if delay:
            # Inject into what the loop ACTUALLY calls per iteration. The first version
            # patched `_probe_wait_targets`, which the event condition never calls (it is
            # only used when condition != "event"), so the control ran at full speed and
            # the probe reported "control 4/5 green" -- a broken control, not a result.
            original_probe = manager.probe_agent
            original_inbox = manager._read_parent_inbox_events_since

            def slow_probe(target):
                time.sleep(delay)
                return original_probe(target)

            def slow_inbox(offset, targets):
                time.sleep(delay)
                return original_inbox(offset, targets)

            manager.probe_agent = slow_probe
            manager._read_parent_inbox_events_since = slow_inbox

        started = time.monotonic()
        manager.wait_agents(["broken"], timeout_s=budget, poll_interval_s=POLL_S)
        elapsed = time.monotonic() - started
        return bool(channel_calls), bool(slept), elapsed


def condition(label: str, delay: float, rounds: int, budget: float = BUDGET_S):
    print(f"\n{label}  (injected per-iteration delay={delay * 1000:.0f} ms, "
          f"budget={budget * 1000:.0f} ms)")
    outcomes: list[tuple[bool, bool]] = []
    times: list[float] = []
    for index in range(rounds):
        reached, ok, elapsed = round_once(delay, budget)
        outcomes.append((reached, ok))
        times.append(elapsed)
        verdict = ("pass" if (reached and ok) else
                   "FAIL (channel never reached -- budget too small)"
                   if not reached else "FAIL (fallback did not run)")
        print(f"  round {index}: channel={'reached' if reached else 'MISSED'} "
              f"slept={'yes' if ok else 'no'}  {elapsed * 1000:.0f} ms  -> {verdict}")
    if times:
        print(f"  median call time: {statistics.median(times) * 1000:.0f} ms")
    return outcomes


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Wall-clock budget probe for wait_agents.")
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--delay", type=float, default=0.03,
                        help="injected per-iteration delay for the control condition")
    args = parser.parse_args(argv[1:])

    print(f"repo       : {REPO}")
    print(f"test budget: {BUDGET_S * 1000:.0f} ms   poll interval: {POLL_S * 1000:.0f} ms")
    print(f"rounds     : {args.rounds}")

    natural = condition("A. NATURAL (current test budget)", 0.0, args.rounds)
    control = condition("B. CONTROL (I/O made slow on purpose)", args.delay, args.rounds)
    fixed = condition("C. NATURAL at the proposed 0.5 s budget", args.delay, args.rounds,
                      budget=0.5)

    print()
    print("VERDICT")
    n_ok = sum(1 for reached, ok in natural if reached and ok)
    c_ok = sum(1 for reached, ok in control if reached and ok)
    f_ok = sum(1 for reached, ok in fixed if reached and ok)
    print(f"  A. 50 ms budget, idle          : {n_ok}/{len(natural)} pass")
    print(f"  B. 50 ms budget, slow iteration: {c_ok}/{len(control)} pass "
          f"({sum(1 for reached, _ in control if not reached)}/{len(control)} never "
          f"reached the channel)")
    print(f"  C. 500 ms budget, slow iteration: {f_ok}/{len(fixed)} pass")
    if c_ok == 0 and f_ok == len(fixed):
        print("  ✔ the control fails only because of the budget, and the same slow")
        print("    iteration passes once the budget covers it. That is the fix, and it is")
        print("    not 'raise the number until green': the new test also asserts the")
        print("    channel was reached, so a future too-small budget reports the real")
        print("    reason instead of blaming the fallback.")
    else:
        print("  ✖ the 0.5 s budget did not survive the control; raise it further.")


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
