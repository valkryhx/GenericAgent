"""The workflow VM must refuse wall-clock and randomness *at runtime*.

The static determinism gate (``determinism.py``) has the same blind spots as
Claude Code's AST check -- aliases, computed member access, ``globalThis`` --
and its own docstring says so. CC 319 closes them with a runtime ``Date`` shim
alongside the static check (``m1325`` ``Ks``/``ShimDate``); GA shipped only the
static half, so a script could still run ``const D = Date; D.now()`` and break
resume. These tests drive the real Node worker, not the Python wrapper.
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WORKER = REPO / "genericagent" / "workflow" / "js" / "worker.js"
META = 'export const meta = { name: "probe", description: "probe" }\n'


def _run(script_body: str, timeout: int = 30) -> dict:
    message = json.dumps(
        {"type": "start", "script": META + script_body, "args": {}, "timeoutMs": 5000}
    )
    proc = subprocess.run(
        ["node", str(WORKER)],
        input=message + "\n",
        capture_output=True,
        text=True,
        encoding="utf-8",
        cwd=str(REPO),
        timeout=timeout,
    )
    events = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    terminal = [event for event in events if event.get("type") in {"done", "error"}]
    assert terminal, f"worker produced no terminal event: {proc.stdout[-500:]} {proc.stderr[-500:]}"
    return terminal[-1]


class WorkerDeterminismShimTest(unittest.TestCase):
    def test_date_now_is_unavailable_at_runtime(self):
        result = _run("return Date.now()")
        self.assertEqual("error", result["type"])
        self.assertIn("Date.now()", result["error"])

    def test_math_random_is_unavailable_at_runtime(self):
        result = _run("return Math.random()")
        self.assertEqual("error", result["type"])
        self.assertIn("Math.random()", result["error"])

    def test_argumentless_new_date_is_unavailable_at_runtime(self):
        result = _run("return new Date()")
        self.assertEqual("error", result["type"])
        self.assertIn("new Date()", result["error"])

    def test_the_static_gate_aliasing_escape_is_closed(self):
        # determinism.py documents this exact bypass; the shim must catch it.
        result = _run("const D = Date; return D.now()")
        self.assertEqual("error", result["type"])
        self.assertIn("Date.now()", result["error"])

    def test_the_computed_member_escape_is_closed(self):
        result = _run('return Date["now"]()')
        self.assertEqual("error", result["type"])
        self.assertIn("Date.now()", result["error"])

    def test_dates_with_an_explicit_value_still_work(self):
        result = _run("return [new Date(0).getTime(), Date.UTC(2020, 0, 1)]")
        self.assertEqual("done", result["type"])
        self.assertEqual([0, 1577836800000], result["result"])

    def test_math_still_works_apart_from_random(self):
        result = _run("return [Math.max(1, 2), Math.round(1.4)]")
        self.assertEqual("done", result["type"])
        self.assertEqual([2, 1], result["result"])


if __name__ == "__main__":
    unittest.main()
