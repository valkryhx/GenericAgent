"""Pytest plugin: make one `wait_agents` loop iteration cost far more than a poll.

Reverse confirmation for `WaitAgentsWatchTest.test_a_broken_channel_falls_back_to_sleeping_instead_of_failing_the_wait`.

The point of the fix is that the 0.05 s budget was too small to contain even one loop
iteration on a loaded machine, so the channel was never reached and the failure blamed
the fallback. A CPU hog does NOT reproduce that (measured: 5/5 green), because what
inflates this loop is filesystem stats and bus reads, not arithmetic.

This plugin makes the condition deterministic without touching product code: it sleeps
inside the two helpers the loop calls every iteration.

    python -m pytest tests/test_subagent_manager.py -p tests.probes.python._slow_iteration_plugin \
        -k broken_channel -q

Acceptance shape:
  * with the fixed test (0.5 s budget)          -> passes
  * after `git stash push -- tests/test_subagent_manager.py` (0.05 s budget) -> fails
    with "the loop never reached the channel", NOT with the old misleading message

The last line is checked with `--slow-iteration-shrink-timeout`, which forces the
budget back down while the fixed test is in place. If the new guard is worth having,
that run must fail on the channel-reached assertion rather than on `slept`.

Three tests share this defect and are covered by the same plugin:

    test_wait_blind_sleeps_when_the_agent_has_no_realtime_channel   (no channel)
    test_wait_sleeps_on_the_channel_when_one_exists                 (live channel)
    test_the_channel_wait_never_overruns_the_deadline               (deadline bound)

Note the shrink only touches calls that already asked for a short budget (<= 1 s).
An earlier version shrank *every* call, which also slammed the 5 s and 30 s tests in
the same class down to 0.05 s and made them fail for a reason unrelated to this probe
-- a reminder that a control which changes more than the variable under study is not a
control.
"""

from __future__ import annotations

import time

INJECTED_DELAY_S = 0.03
SHRINK_TIMEOUT_TO: float | None = None


def pytest_addoption(parser):
    parser.addoption(
        "--slow-iteration-shrink-timeout", action="store",
        default=None, metavar="SECONDS",
        help="force wait_agents' timeout_s down to this value (diagnostic quality check)",
    )


def pytest_configure(config):
    global SHRINK_TIMEOUT_TO
    from genericagent.agents.manager import SubagentManager

    shrink = config.getoption("--slow-iteration-shrink-timeout")
    SHRINK_TIMEOUT_TO = float(shrink) if shrink else None

    original_probe = SubagentManager.probe_agent
    original_inbox = SubagentManager._read_parent_inbox_events_since
    original_wait = SubagentManager.wait_agents

    def slow_probe(self, target):
        time.sleep(INJECTED_DELAY_S)
        return original_probe(self, target)

    def slow_inbox(self, offset, targets):
        time.sleep(INJECTED_DELAY_S)
        return original_inbox(self, offset, targets)

    def shrunk_wait(self, targets=None, timeout_s=30, **kwargs):
        # Only shrink calls that already asked for a short budget. Shrinking everything
        # also collapsed the 5 s / 30 s tests in the same class down to 0.05 s, and they
        # then failed for a reason that has nothing to do with the budget under study.
        if SHRINK_TIMEOUT_TO is not None and 0 < timeout_s <= 1.0:
            timeout_s = SHRINK_TIMEOUT_TO
        return original_wait(self, targets, timeout_s, **kwargs)

    SubagentManager.probe_agent = slow_probe
    SubagentManager._read_parent_inbox_events_since = slow_inbox
    SubagentManager.wait_agents = shrunk_wait
