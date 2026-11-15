"""How long does `abort()` actually take to clear `is_running`, and what does the
0.75 s window in `test_abort_stops_mcp_discovery_before_first_model_request` cover?

The test's shape
----------------
    agent.abort()                                   # main thread
    deadline = time.monotonic() + 0.75
    while agent.is_running and time.monotonic() < deadline: sleep(0.01)
    stopped_quickly = not agent.is_running

`is_running` is set to True at `app.py:1999` (turn start) and cleared only in the
`finally` at `app.py:2328` — *after* the whole turn unwinds. So the 0.75 s window does
not measure "did abort stop discovery". It measures:

    handler.cancel()                         <- what the test claims to check
      -> blocking load_tool_schema notices   (<= one 0.01 s poll)
      -> returns, builds capability profile
      -> constructs agent_runner_loop
      -> _start_stop_file_watcher()
      -> _drain_agent_stream(...)            <- whole runner teardown
      -> except/finally bookkeeping
      -> is_running = False                  <- what the test actually measures

Only the first step is the behavior under test; the rest is unrelated teardown that
grows with machine load. That gap is the flake.

This probe separates the two, so the fix can assert the mechanism instead of the
teardown:

  t_abort_noticed  — time until the blocking discovery stub saw the stop signal and
                     returned. This IS "abort stops MCP discovery".
  t_turn_cleared   — time until `is_running` became False. This is teardown, and it is
                     the only part that the 0.75 s budget was (accidentally) covering.

The control injects a fixed delay into the teardown path so the second number is
provably able to exceed 0.75 s while the first stays small — that is the deterministic
demonstration that the assertion can fail for a reason unrelated to its own claim.

Usage
-----
    python tests/probes/python/_probe_abort_discovery_latency.py
    python tests/probes/python/_probe_abort_discovery_latency.py --rounds 5 --teardown-delay 0.9

Exit code is always 0: this is a measurement. Read the verdict lines.
"""

from __future__ import annotations

import argparse
import queue
import statistics
import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

REPO = Path(__file__).resolve().parents[3]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

TEST_BUDGET_S = 0.75   # what the test allows between abort() and `is_running` == False
DISCOVERY_WAIT_S = 2.0  # what the test allows for discovery to start


def _run_once(teardown_delay: float) -> tuple[float | None, float | None]:
    """Reproduce the test body once.

    Returns (abort->discovery-noticed, abort->is_running-cleared), or None for a
    timing that never happened.
    """
    from genericagent.core.app import GenericAgent

    discovery_started = threading.Event()
    discovery_noticed_abort = threading.Event()
    release_discovery = threading.Event()
    stamps: dict[str, float] = {}

    class StubClient:
        def __init__(self):
            self.backend = type("Backend", (), {
                "history": [], "extra_sys_prompt": "", "model": "gpt-test",
                "cancelled": False,
            })()
            self.last_tools = ""
            self.chat_calls = 0

        def reset_cancel(self):
            self.backend.cancelled = False

        def cancel_current_request(self):
            self.backend.cancelled = True

        def chat(self, messages, tools=None):
            self.chat_calls += 1
            raise AssertionError("cancelled discovery must not reach the model")

    client = StubClient()
    agent = GenericAgent.__new__(GenericAgent)
    agent.lock = threading.Lock()
    agent.task_dir = None
    agent.history = []
    agent.handler = None
    agent.task_queue = queue.Queue()
    agent.is_running = False
    agent.stop_sig = False
    agent.llm_no = 0
    agent.inc_out = False
    agent.verbose = False
    agent.peer_hint = False
    agent.log_path = str(REPO / "temp" / "probe-abort-discovery.log")
    agent.llmclient = client
    agent.llmclients = [client]

    def blocking_load_tool_schema(*_args, **_kwargs):
        from genericagent.mcp.runtime import _current_stop_signal

        discovery_started.set()
        while not release_discovery.is_set():
            stop_signal = _current_stop_signal()
            if stop_signal is not None and stop_signal.is_set():
                stamps["discovery_returned"] = time.monotonic()
                discovery_noticed_abort.set()
                # Control condition: pretend the rest of the turn is slow. This is the
                # teardown, NOT the behavior under test.
                if teardown_delay:
                    time.sleep(teardown_delay)
                return
            time.sleep(0.01)

    globals_ref = GenericAgent.run.__globals__

    def watching_is_running():
        return bool(getattr(agent, "is_running", False))

    worker = threading.Thread(target=agent.run, daemon=True)
    noticed = cleared = None
    with patch.dict(globals_ref, {
        "get_system_prompt": lambda *_args, **_kwargs: "",
        "load_tool_schema": blocking_load_tool_schema,
    }), patch.object(globals_ref["session_transcript"], "current_backend_history", return_value=[]), patch.object(
        globals_ref["session_transcript"], "record_agent_turn", return_value=None
    ):
        worker.start()
        agent.put_task("trigger discovery")
        if not discovery_started.wait(timeout=DISCOVERY_WAIT_S):
            release_discovery.set()
            return None, None
        stamps["abort_called"] = time.monotonic()
        agent.abort()
        if discovery_noticed_abort.wait(timeout=10.0):
            noticed = stamps["discovery_returned"] - stamps["abort_called"]
        deadline = time.monotonic() + 15.0
        while watching_is_running() and time.monotonic() < deadline:
            time.sleep(0.005)
        if not watching_is_running():
            cleared = time.monotonic() - stamps["abort_called"]
        release_discovery.set()
        end = time.monotonic() + 5.0
        while watching_is_running() and time.monotonic() < end:
            time.sleep(0.01)
    return noticed, cleared


def condition(label: str, teardown_delay: float, rounds: int):
    print(f"\n{label}  (injected teardown delay={teardown_delay * 1000:.0f} ms)")
    noticed_rows: list[float] = []
    cleared_rows: list[float] = []
    for index in range(rounds):
        noticed, cleared = _run_once(teardown_delay)
        if noticed is None:
            print(f"  round {index}: discovery never observed the abort")
            continue
        noticed_rows.append(noticed)
        over = cleared is not None and cleared > TEST_BUDGET_S
        if cleared is not None:
            cleared_rows.append(cleared)
        print(f"  round {index}: abort->discovery {noticed * 1000:6.0f} ms   "
              f"abort->is_running {('%.0f ms' % (cleared * 1000)) if cleared is not None else 'never'}   "
              f"{'WOULD FAIL (>%.0f ms)' % (TEST_BUDGET_S * 1000) if over else 'would pass'}")
    if noticed_rows:
        print(f"  median abort->discovery : {statistics.median(noticed_rows) * 1000:.0f} ms")
    if cleared_rows:
        print(f"  median abort->teardown  : {statistics.median(cleared_rows) * 1000:.0f} ms")
    return noticed_rows, cleared_rows


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="abort() -> MCP discovery stop latency.")
    parser.add_argument("--rounds", type=int, default=3)
    parser.add_argument("--teardown-delay", type=float, default=0.9,
                        help="injected delay for the control condition (must exceed the budget)")
    args = parser.parse_args(argv[1:])

    print(f"repo      : {REPO}")
    print(f"test budget (abort -> is_running cleared): {TEST_BUDGET_S * 1000:.0f} ms")
    print(f"rounds    : {args.rounds}")

    n_natural, c_natural = condition("A. NATURAL", 0.0, args.rounds)
    n_control, c_control = condition("B. CONTROL (teardown made slow on purpose)",
                                     args.teardown_delay, args.rounds)

    print()
    print("VERDICT")
    print(f"  natural: discovery noticed abort {len(n_natural)}/{args.rounds}, "
          f"teardown under budget {sum(1 for v in c_natural if v <= TEST_BUDGET_S)}/{len(c_natural)}")
    print(f"  control: discovery noticed abort {len(n_control)}/{args.rounds}, "
          f"teardown under budget {sum(1 for v in c_control if v <= TEST_BUDGET_S)}/{len(c_control)}")
    if n_control and all(v <= TEST_BUDGET_S * 0.5 for v in n_control) and c_control and all(
            v > TEST_BUDGET_S for v in c_control):
        print("  ✔ the mechanism under test (abort stops discovery) stays fast in the control,")
        print("    while the quantity the assertion measures (is_running cleared) blows the")
        print("    budget. So `stopped_quickly = not agent.is_running` can go red without")
        print("    discovery-interruption being broken -- it is measuring turn teardown.")
        print("    Fix: assert the discovery stub observed the stop signal, and give the")
        print("    teardown its own, larger budget.")
    else:
        print("  ✖ the control did not separate the two timings; raise --teardown-delay.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
