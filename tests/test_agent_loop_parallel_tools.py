import json
import sys
import threading
import time
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from agent_loop import (  # noqa: E402
    StepOutcome,
    _batch_tool_calls,
    _tool_execution_mode,
    agent_runner_loop,
)


class _ToolCall:
    def __init__(self, name, arguments="{}", call_id=None):
        self.function = type("Fn", (), {"name": name, "arguments": arguments})()
        self.id = call_id or ("call_" + name)


class _Response:
    def __init__(self, tool_calls, content=""):
        self.tool_calls = tool_calls
        self.content = content


class _Client:
    """Returns one scripted response per turn, then a tool-free response."""

    def __init__(self, responses):
        self.responses = list(responses)
        self.last_tools = ""
        self.turns = 0
        self.results = []

    def chat(self, messages=None, tools=None):
        self.turns += 1
        response = self.responses.pop(0) if self.responses else _Response([])
        if False:
            yield ""
        return response


class _Handler:
    """Dispatch stub that records call order and can sleep per tool."""

    max_turns = 5

    def __init__(self, delays=None, fail=None):
        self.parent = type("Parent", (), {"task_dir": None})()
        self._done_hooks = []
        self.current_turn = 0
        self.calls = []
        self.delays = delays or {}
        self.fail = fail or {}
        self.lock = threading.Lock()
        self.client = None

    def dispatch(self, tool_name, args, response, index=0, tool_num=1):
        with self.lock:
            self.calls.append(tool_name)
        delay = self.delays.get(tool_name, 0)
        if delay:
            time.sleep(delay)
        if tool_name in self.fail:
            raise RuntimeError(self.fail[tool_name])
        yield tool_name + " output\n"
        return StepOutcome({"tool": tool_name}, next_prompt="\n")

    def turn_end_callback(self, response, tool_calls, tool_results, turn, next_prompt, exit_reason):
        if self.client is not None:
            self.client.results.extend(tool_results)
        return ""


def _run(client, handler, verbose=False):
    handler.client = client
    return list(agent_runner_loop(client, "sys", "go", handler, [], max_turns=2, verbose=verbose))


class ToolExecutionModeTest(unittest.TestCase):
    """Only side-effect-free tools may overlap; everything else stays ordered."""

    def test_mcp_tools_are_parallel_safe(self):
        self.assertEqual(_tool_execution_mode("mcp__tavily__tavily_search"), "parallel")

    def test_file_read_is_parallel_safe(self):
        self.assertEqual(_tool_execution_mode("file_read"), "parallel")

    def test_side_effecting_tools_stay_sequential(self):
        names = ("code_run", "file_write", "file_patch", "web_execute_js", "ask_user",
                 "spawn_agent", "close_agent", "update_working_checkpoint", "no_tool")
        for name in names:
            self.assertEqual(_tool_execution_mode(name), "sequential", name)

    def test_batches_keep_order_and_split_on_mode_change(self):
        calls = [
            {"tool_name": "mcp__a__x"},
            {"tool_name": "mcp__b__y"},
            {"tool_name": "code_run"},
            {"tool_name": "file_read"},
            {"tool_name": "mcp__c__z"},
        ]
        shapes = [(mode, [i for i, _ in batch]) for mode, batch in _batch_tool_calls(calls)]
        self.assertEqual(shapes, [
            ("parallel", [0, 1]),
            ("sequential", [2]),
            ("parallel", [3, 4]),
        ])


class ParallelToolExecutionTest(unittest.TestCase):
    """Overlapping MCP calls must actually overlap and still report in order."""

    def test_parallel_batch_overlaps_instead_of_serialising(self):
        handler = _Handler(delays={"mcp__s__a": 0.4, "mcp__s__b": 0.4})
        client = _Client([_Response([
            _ToolCall("mcp__s__a", call_id="a"),
            _ToolCall("mcp__s__b", call_id="b"),
        ])])

        started = time.monotonic()
        _run(client, handler)
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, 0.75, "two 0.4s calls took %.2fs, so they ran serially" % elapsed)

    def test_parallel_batch_preserves_result_order(self):
        handler = _Handler(delays={"mcp__s__slow": 0.3})
        client = _Client([_Response([
            _ToolCall("mcp__s__slow", call_id="slow"),
            _ToolCall("mcp__s__fast", call_id="fast"),
        ])])

        _run(client, handler)

        self.assertEqual([c["tool_use_id"] for c in client.results], ["slow", "fast"])

    def test_side_effecting_tools_keep_sequential_order(self):
        handler = _Handler()
        client = _Client([_Response([
            _ToolCall("code_run", call_id="1"),
            _ToolCall("file_write", call_id="2"),
        ])])

        _run(client, handler)

        # Later turns fall back to no_tool; only the scripted turn matters here.
        self.assertEqual(handler.calls[:2], ["code_run", "file_write"])

    def test_single_parallel_call_does_not_spawn_threads(self):
        handler = _Handler()
        before = threading.active_count()
        client = _Client([_Response([_ToolCall("mcp__s__only")])])
        _run(client, handler)
        self.assertEqual(threading.active_count(), before)

    def test_verbose_mode_keeps_streaming_output(self):
        handler = _Handler()
        client = _Client([_Response([_ToolCall("mcp__s__a")])])
        chunks = _run(client, handler, verbose=True)
        joined = "".join(str(c) for c in chunks)
        self.assertIn("mcp__s__a output", joined)
        self.assertIn("`````", joined)

    def test_tool_exception_propagates_out_of_parallel_batch(self):
        handler = _Handler(fail={"mcp__s__bad": "boom"})
        client = _Client([_Response([_ToolCall("mcp__s__bad", call_id="bad")])])
        with self.assertRaises(RuntimeError):
            _run(client, handler)


if __name__ == "__main__":
    unittest.main()
