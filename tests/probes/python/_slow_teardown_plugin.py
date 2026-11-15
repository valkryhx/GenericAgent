"""Pytest plugin: make the aborted turn's teardown slow, to show the old assertion
in `test_abort_stops_mcp_discovery_before_first_model_request` measured teardown,
not discovery interruption.

The test used to do:

    agent.abort()
    deadline = time.monotonic() + 0.75
    while agent.is_running and time.monotonic() < deadline: sleep(0.01)
    stopped_quickly = not agent.is_running

`is_running` clears only in the `finally` at app.py:2328, after the entire turn
unwinds. So that loop measured teardown. This plugin delays one step of the unwind
(the stop-file watcher setup that runs right after the capability profile is built),
which makes teardown provably exceed 0.75 s while abort's reach into the discovery
loop is untouched.

    python -m pytest tests/test_agentmain_llm_sessions.py \
        -p tests.probes.python._slow_teardown_plugin -k discovery -q

Acceptance shape:
  * fixed test (asserts the discovery loop saw the abort)  -> passes
  * `git stash push -- tests/test_agentmain_llm_sessions.py` (asserts is_running
    cleared in 0.75 s)                                    -> fails, with a message
    about abort not interrupting discovery, which is false
"""

from __future__ import annotations

import time

INJECTED_TEARDOWN_DELAY_S = 0.9


def pytest_configure(config):
    from genericagent.core import app as app_module

    original = app_module.GenericAgent._start_stop_file_watcher

    def slow_start_stop_file_watcher(self, *args, **kwargs):
        # This runs after MCP discovery has already returned, i.e. strictly after the
        # behaviour the test claims to check.
        time.sleep(INJECTED_TEARDOWN_DELAY_S)
        return original(self, *args, **kwargs)

    app_module.GenericAgent._start_stop_file_watcher = slow_start_stop_file_watcher
