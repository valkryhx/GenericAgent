"""Do the *other* WaitAgentsWatchTest cases share the 50 ms budget defect?

`test_wait_blind_sleeps_when_the_agent_has_no_realtime_channel` failed in a full-suite
run after the first fix landed, with the same shape as the one already repaired:

    result = manager.wait_agents(["filed"], timeout_s=0.05, poll_interval_s=0.02)
    self.assertTrue(result.timed_out)
    self.assertTrue(slept, "the file transport lost its poll loop")

Every one of these tests asserts on something the loop only reaches *after* a full
iteration (probe the target, read the event bus, read the parent inbox, walk the
targets, then `_wait_for_change`). At 50 ms that iteration has no headroom, so on a
loaded machine the deadline fires first and the assertion reports the mechanism as
broken when the loop never got there.

Three shapes are covered, because they fail for the same reason through different
branches of `_wait_for_change`:

  A. no channel        -> the `not channels` blind-sleep branch
  B. live channel      -> the `channel.wait_for_signal()` branch
  C. broken channel    -> the `except` fallback that degrades to sleeping

A deterministic control is built by sleeping inside the two helpers the loop calls
every iteration (30 ms each), which makes one iteration provably exceed the budget
without patching product code.

Usage
-----
    python tests/probes/python/_probe_wait_budget_sweep.py
    python tests/probes/python/_probe_wait_budget_sweep.py --rounds 6 --fixed-budget 0.5
"""

from __future__ import annotations

import argparse
import json
import statistics
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
for _path in (REPO, REPO / "tests"):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

TEST_BUDGET_S = 0.05
POLL_S = 0.02


class _NoChannel:
    """No realtime channel registered: `_wait_for_change` blind-sleeps."""


class _SilentChannel:
    def __init__(self):
        self.calls: list[float] = []

    def wait_for_signal(self, timeout):
        self.calls.append(timeout)
        return False


class _BrokenChannel:
    def __init__(self):
        self.calls: list[float] = []

    def wait_for_signal(self, timeout):
        self.calls.append(timeout)
        raise OSError("channel is gone")


def _build(td: str, kind: str):
    from genericagent.agents.manager import SubagentManager

    task_dir = Path(td) / "temp" / "target"
    task_dir.mkdir(parents=True)
    (task_dir / "output.txt").write_text("working", encoding="utf-8")
    (task_dir / "state.json").write_text(json.dumps({
        "schema_version": 1, "task_name": "target", "agent_path": "/root/target",
        "pid": 500, "round": 0, "turn_status": "running", "process_status": "alive",
        "task_dir": str(task_dir), "input_path": str(task_dir / "input.txt"),
        "output_path": str(task_dir / "output.txt"),
        "started_at": "2026-01-01T00:00:00", "updated_at": "2026-01-01T00:00:00",
    }), encoding="utf-8")

    slept: list[float] = []
    manager = SubagentManager(root_dir=td, process_exists=lambda _pid: True,
                              sleep=slept.append)
    channel = None
    if kind == "live":
        channel = _SilentChannel()
    elif kind == "broken":
        channel = _BrokenChannel()
    if channel is not None:
        manager._realtime_channels["/root/target"] = channel
    return manager, slept, channel


def run_case(kind: str, budget: float, delay: float) -> tuple[bool, bool, float]:
    """Returns (reached_wait_for_change, observed_expected_effect, elapsed)."""
    with tempfile.TemporaryDirectory() as td:
        manager, slept, channel = _build(td, kind)

        reached = {"flag": False}
        original_change = manager._wait_for_change

        def marking_change(targets, poll_interval_s, deadline):
            reached["flag"] = True
            return original_change(targets, poll_interval_s, deadline)

        manager._wait_for_change = marking_change

        if delay:
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
        manager.wait_agents(["target"], timeout_s=budget, poll_interval_s=POLL_S)
        elapsed = time.monotonic() - started

        if kind == "no-channel":
            effect = bool(slept)
        else:
            effect = bool(channel.calls)
        return reached["flag"], effect, elapsed


CASES = {
    "no-channel": "test_wait_blind_sleeps_when_the_agent_has_no_realtime_channel",
    "live": "test_wait_sleeps_on_the_channel_when_one_exists",
    "broken": "test_a_broken_channel_falls_back_to_sleeping_instead_of_failing_the_wait",
}


def condition(label: str, budget: float, delay: float, rounds: int, kinds) -> dict:
    print(f"\n{label}  (budget={budget * 1000:.0f} ms, injected delay={delay * 1000:.0f} ms)")
    out: dict[str, list[tuple[bool, bool]]] = {k: [] for k in kinds}
    for kind in kinds:
        times = []
        for _ in range(rounds):
            reached, effect, elapsed = run_case(kind, budget, delay)
            out[kind].append((reached, effect))
            times.append(elapsed)
        passes = sum(1 for reached, effect in out[kind] if reached and effect)
        misses = sum(1 for reached, _ in out[kind] if not reached)
        print(f"  {kind:11s} pass {passes}/{rounds}"
              f"   (loop never reached _wait_for_change: {misses}/{rounds})"
              f"   median {statistics.median(times) * 1000:.0f} ms")
    return out


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Sweep the wait_agents budget across branches.")
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--delay", type=float, default=0.03,
                        help="injected per-iteration delay used to build the control")
    parser.add_argument("--fixed-budget", type=float, default=0.5,
                        help="the budget proposed as the fix")
    args = parser.parse_args(argv[1:])

    kinds = list(CASES)
    print(f"repo        : {REPO}")
    print(f"test budget : {TEST_BUDGET_S * 1000:.0f} ms   poll interval: {POLL_S * 1000:.0f} ms")
    print(f"cases       :")
    for kind in kinds:
        print(f"  {kind:11s} -> {CASES[kind]}")

    natural = condition("A. NATURAL, current budget", TEST_BUDGET_S, 0.0, args.rounds, kinds)
    control = condition("B. CONTROL, current budget (slow iteration)",
                        TEST_BUDGET_S, args.delay, args.rounds, kinds)
    fixed = condition(f"C. NATURAL, {args.fixed_budget:.1f} s budget (slow iteration)",
                      args.fixed_budget, args.delay, args.rounds, kinds)

    print()
    print("VERDICT")
    all_ok = True
    for kind in kinds:
        n = sum(1 for r, e in natural[kind] if r and e)
        c = sum(1 for r, e in control[kind] if r and e)
        f = sum(1 for r, e in fixed[kind] if r and e)
        verdict = "✔" if (c == 0 and f == args.rounds) else "✖"
        if verdict == "✖":
            all_ok = False
        print(f"  {verdict} {kind:11s} idle {n}/{args.rounds}   control {c}/{args.rounds}   fixed {f}/{args.rounds}")
    if all_ok:
        print("\n  All three branches fail on the control and pass at the larger budget,")
        print("  so they share one defect: the budget must cover one whole loop iteration")
        print("  (probe -> bus -> inbox -> walk targets -> _wait_for_change), not just the")
        print("  final wait. Sizing it to that is the fix; the assertions themselves are")
        print("  correct about the mechanism they name.")
    else:
        print("\n  A branch did not behave as predicted -- investigate before changing tests.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
