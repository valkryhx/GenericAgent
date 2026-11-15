"""Which tests go flaky under parallel/CPU load, and by what mechanism?

What it measures
----------------
For each timing-sensitive test, the pass rate under two conditions:

  * **idle** — nothing else running;
  * **loaded** — N background CPU hogs, which is what a parallel `pytest -n` run,
    a full-suite run, or simply two test processes at once looks like to the test.

The comparison is the point. A test that fails only under load has a budget smaller
than its own noise; a test that fails when idle has a real defect. Reporting one
sample per condition proves nothing (CLAUDE.md rule 5), so every cell is a rate over
N rounds.

Root cause of every entry here
------------------------------
All of them assert on a wall-clock quantity while the thing being timed shares its
budget with a fixed setup cost:

* ``test_runtime_timeout_cancels_running_child_agents`` — ``WorkflowRuntime`` starts
  its deadline before the node worker exists (``runtime.py:284``), so a 0.2 s budget
  also pays process spawn + ``ready`` handshake (measured 145–525 ms idle, 1.4 s
  loaded). Fixed 2026-11-16 by raising the budget and asserting the child started.
* ``test_cancelled_agent_never_settles_so_script_does_not_advance`` and friends —
  same mechanism, budget already 2.0 s; survives normal load, not extreme load.
* ``test_run_git_timeout_kills_pipe_holding_descendants`` — asserts the kill lands
  within 1.5 s of a 0.2 s timeout; on a saturated scheduler the cleanup itself is
  starved.
* ``test_a_stalled_subscriber_does_not_delay_delivery_to_a_healthy_one`` — asserts
  >=100 of 200 events arrive inside a 3 s drain window; a starved sender thread
  undershoots.
* ``test_cancelled_waiter_does_not_cancel_shared_server_startup`` /
  ``test_concurrent_discovery_reuses_shared_server_startup`` — real MCP subprocess
  startup inside a bounded wait.

Why it must be kept
-------------------
"Flaky under load" is the easy explanation and it was wrong once already in this
repo: the 0.2 s workflow budget was called existing noise until the measurement
showed the budget was *below the floor* of a fixed startup cost, i.e. it failed for
a reason unrelated to what it claimed to test. This probe is the standing record of
which failures are genuinely load-induced and which needed a fix.

Usage
-----
    python tests/probes/python/_probe_test_flakiness_under_load.py
    python tests/probes/python/_probe_test_flakiness_under_load.py --rounds 8 --hogs 6

Exit code is always 0: this is a measurement. Read the table.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]

CANDIDATES = [
    (
        "workflow:cancel-semantics",
        "tests/test_workflow_runtime.py::WorkflowCancellationSemanticsTest"
        "::test_cancelled_agent_never_settles_so_script_does_not_advance",
    ),
    (
        "workflow:timeout-cancels-children",
        "tests/test_workflow_runtime.py::WorkflowRuntimeTest"
        "::test_runtime_timeout_cancels_running_child_agents",
    ),
    (
        "subagent:worktree-kill-1.5s",
        "tests/test_subagent_worktree.py::SubagentWorktreeTest"
        "::test_run_git_timeout_kills_pipe_holding_descendants",
    ),
    (
        "subagent:ipc-healthy-subscriber",
        "tests/test_subagent_realtime_ipc.py::ChannelSlowSubscriberTest"
        "::test_a_stalled_subscriber_does_not_delay_delivery_to_a_healthy_one",
    ),
    (
        "subagent:mcp-cancel-waiter",
        "tests/test_mcp_runtime.py::McpRuntimeTest"
        "::test_cancelled_waiter_does_not_cancel_shared_server_startup",
    ),
    (
        "subagent:mcp-concurrent-discovery",
        "tests/test_mcp_runtime.py::McpRuntimeTest"
        "::test_concurrent_discovery_reuses_shared_server_startup",
    ),
]

_HOG = """
import time
end = time.time() + float(__import__('sys').argv[1])
while time.time() < end:
    pass
"""


def run_test(nodeid: str) -> tuple[bool, float, str]:
    started = time.monotonic()
    proc = subprocess.run(
        [sys.executable, "-m", "pytest", nodeid, "-q", "-p", "no:cacheprovider"],
        capture_output=True, text=True, cwd=str(REPO),
    )
    elapsed = time.monotonic() - started
    combined = f"{proc.stdout}\n{proc.stderr}"
    passed = " passed" in combined and " failed" not in combined
    reason = ""
    if not passed:
        for line in combined.splitlines():
            if line.startswith("E   ") or "AssertionError" in line:
                reason = line.strip()[:90]
                break
    return passed, elapsed, reason


def measure(idle: bool, rounds: int, hogs: int) -> dict[str, dict]:
    """Pass rate per candidate. Runs serially so the condition is the only variable."""
    out: dict[str, dict] = {label: {"pass": 0, "rounds": 0, "reasons": [], "elapsed": []}
                            for label, _ in CANDIDATES}
    for label, nodeid in CANDIDATES:
        for _ in range(rounds):
            passed, elapsed, reason = run_test(nodeid)
            cell = out[label]
            cell["rounds"] += 1
            cell["elapsed"].append(elapsed)
            if passed:
                cell["pass"] += 1
            elif reason:
                cell["reasons"].append(reason)
    return out


def start_hogs(count: int, seconds: float) -> list[subprocess.Popen]:
    procs = []
    for _ in range(count):
        procs.append(subprocess.Popen([sys.executable, "-c", _HOG, str(seconds)],
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
    return procs


def stop_hogs(procs: list[subprocess.Popen]) -> None:
    for proc in procs:
        try:
            proc.kill()
        except Exception:
            pass


def print_table(title: str, data: dict[str, dict]) -> None:
    print(f"\n{title}")
    print(f"  {'test':34} {'pass':>8}  {'avg s':>7}  most common failure")
    for label, _ in CANDIDATES:
        cell = data[label]
        avg = sum(cell["elapsed"]) / max(1, len(cell["elapsed"]))
        reason = cell["reasons"][0] if cell["reasons"] else ""
        print(f"  {label:34} {cell['pass']}/{cell['rounds']:<6} {avg:7.2f}  {reason}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Pass rates of timing-sensitive tests, idle vs loaded.")
    parser.add_argument("--rounds", type=int, default=6, help="rounds per candidate per condition")
    parser.add_argument("--hogs", type=int, default=6, help="background CPU hogs for the loaded condition")
    parser.add_argument("--seconds", type=float, default=600.0, help="how long the hogs run")
    parser.add_argument("--skip-idle", action="store_true", help="only measure the loaded condition")
    args = parser.parse_args(argv[1:])

    print(f"repo    : {REPO}")
    print(f"rounds  : {args.rounds} per candidate per condition")
    print(f"hogs    : {args.hogs} CPU-bound processes for the loaded condition")

    if not args.skip_idle:
        print("\n[1/2] idle ...")
        print_table("IDLE", measure(idle=True, rounds=args.rounds, hogs=0))

    print("\n[2/2] loaded ...")
    hogs = start_hogs(args.hogs, args.seconds)
    try:
        loaded = measure(idle=False, rounds=args.rounds, hogs=args.hogs)
    finally:
        stop_hogs(hogs)
    print_table("LOADED", loaded)

    print()
    print("How to read this")
    print("  A test that is green idle and red loaded has a budget smaller than its")
    print("  own scheduling noise: widen the budget, or assert on the mechanism")
    print("  (e.g. 'the child started') instead of on elapsed time.")
    print("  A test that is red even when idle has a real defect, not a flake.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
