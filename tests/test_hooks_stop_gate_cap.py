"""Stop gate consecutive-block cap = CC CLAUDE_CODE_STOP_HOOK_BLOCK_CAP.

CC 303: m0039:2817 ``mi = D.int()`` exports CLAUDE_CODE_STOP_HOOK_BLOCK_CAP;
m0671:8983 ``fe = env.CLAUDE_CODE_STOP_HOOK_BLOCK_CAP ?? 8`` then
``if (fe > 0 && me > fe)`` force-ends the turn. GA mirrors that: default 8,
configurable, ``<=0`` disables the cap, and goal shares the same cap.
"""
import unittest
from unittest import mock

from genericagent.core import handler as handler_mod
from genericagent.core.handler import GenericAgentHandler
from genericagent.hooks.models import HookAggregate
from genericagent.hooks import runtime as hooks_runtime


def _block_agg(reason="keep going"):
    a = HookAggregate(event="Stop")
    a.decision = "block"
    a.reason = reason
    return a


class _AlwaysBlockRuntime:
    def __init__(self):
        self.stop_calls = 0

    def run_event(self, agent, event, data=None, *, emit=None):
        if event == "Stop":
            self.stop_calls += 1
            return _block_agg()
        return HookAggregate(event=event)


def _make_handler(runtime):
    h = GenericAgentHandler.__new__(GenericAgentHandler)
    h.parent = mock.Mock()
    h.parent.is_subagent = False
    h.parent.hook_runtime = runtime
    h.hook_runtime = runtime
    h.hook_parent_agent = None
    h.permission_runtime = None
    h.last_hook_aggregate = None
    h._stop_gate_count = 0
    h.stop_emitted = False
    return h


def _patch_runtime(runtime):
    """Serve Stop through the shared entry point (like the real gate does)."""
    return mock.patch("genericagent.hooks.runtime.run_hook_event",
                      side_effect=lambda agent, event, data=None, *, emit=None:
                      runtime.run_event(agent, event, data, emit=emit))


def _run_gate(runtime, times):
    h = _make_handler(runtime)
    with _patch_runtime(runtime):
        return [h.run_stop_gate({})[0] for _ in range(times)]


class StopGateCapTests(unittest.TestCase):
    def test_default_cap_is_eight(self):
        rt = _AlwaysBlockRuntime()
        actions = _run_gate(rt, 10)
        self.assertEqual(actions[:8], ["continue"] * 8)
        self.assertEqual(actions[8:], ["none", "none"])
        self.assertEqual(rt.stop_calls, 8)  # no Stop emission past the cap

    def test_env_overrides_cap(self):
        rt = _AlwaysBlockRuntime()
        with mock.patch.dict("os.environ", {"CLAUDE_CODE_STOP_HOOK_BLOCK_CAP": "3"}):
            actions = _run_gate(rt, 6)
        self.assertEqual(actions[:3], ["continue"] * 3)
        self.assertEqual(actions[3:], ["none"] * 3)
        self.assertEqual(rt.stop_calls, 3)

    def test_zero_or_negative_disables_cap(self):
        for raw in ("0", "-1"):
            rt = _AlwaysBlockRuntime()
            with mock.patch.dict("os.environ", {"CLAUDE_CODE_STOP_HOOK_BLOCK_CAP": raw}):
                actions = _run_gate(rt, 12)
            # cap disabled -> every call reaches the block hook
            self.assertEqual(actions, ["continue"] * 12, raw)
            self.assertEqual(rt.stop_calls, 12, raw)

    def test_invalid_env_falls_back_to_default(self):
        for raw in ("abc", "", "  "):
            rt = _AlwaysBlockRuntime()
            with mock.patch.dict("os.environ", {"CLAUDE_CODE_STOP_HOOK_BLOCK_CAP": raw}):
                actions = _run_gate(rt, 9)
            self.assertEqual(actions[:8], ["continue"] * 8, raw)
            self.assertEqual(actions[8], "none", raw)

    def test_explicit_max_checks_overrides_env(self):
        rt = _AlwaysBlockRuntime()
        h = _make_handler(rt)
        with _patch_runtime(rt), mock.patch.dict("os.environ", {"CLAUDE_CODE_STOP_HOOK_BLOCK_CAP": "1"}):
            actions = [h.run_stop_gate({}, max_checks=2)[0] for _ in range(4)]
        self.assertEqual(actions, ["continue", "continue", "none", "none"])

    def test_helper_parses_env(self):
        self.assertEqual(handler_mod._stop_hook_block_cap(), 8)
        with mock.patch.dict("os.environ", {"CLAUDE_CODE_STOP_HOOK_BLOCK_CAP": "5"}):
            self.assertEqual(handler_mod._stop_hook_block_cap(), 5)
        with mock.patch.dict("os.environ", {"CLAUDE_CODE_STOP_HOOK_BLOCK_CAP": "0"}):
            self.assertEqual(handler_mod._stop_hook_block_cap(), 0)


if __name__ == "__main__":
    unittest.main()
