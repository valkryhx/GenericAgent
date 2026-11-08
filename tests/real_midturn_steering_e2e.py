"""Real evidence: mid-turn input reaches the model in the same turn.

The unit tests prove the queue and the fold point. This proves the thing the user
actually cares about: that a message pushed *while the turn is running* comes back out
of the model in that same turn.

How it avoids faking itself:

  * the real ``agent_runner_loop`` runs, with a real ``GenericAgentHandler`` and a real
    model client -- no stub client, no canned response;
  * the model is given a task that spans at least two tool calls, so the loop passes
    through a genuine fold point;
  * the steering message carries a random token that appears nowhere else: not in the
    system prompt, not in the task, not in any tool output;
  * the assertion is that the token shows up in the model's *own* text, which it could
    only have learned from the folded-in message.

A test that only checked "the queue drained" would pass even if the text went to a dead
letter slot. That is why the token is the judgement.

Run:
    $env:GA_RUN_REAL_API_E2E="1"; python tests/real_midturn_steering_e2e.py
"""

from __future__ import annotations

import contextlib
import io
import os
import random
import string
import sys
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

OPT_IN = os.environ.get("GA_RUN_REAL_API_E2E") == "1"
REPORT = Path("D:/tmp/ga_real_midturn_steering.txt")


def token(prefix):
    alphabet = string.ascii_uppercase + string.digits
    return prefix + "".join(random.choice(alphabet) for _ in range(10))


class _StepTrackingHandler:
    """Wrap the real handler so the steering message lands at a real fold point.

    The loop calls ``handler.dispatch(name, args, ...)``, not ``do_<name>`` directly, so
    the interception has to happen in ``dispatch`` -- overriding ``do_code_run`` looked
    right and silently never fired.
    """

    def __init__(self, inner, push, after_tool=2):
        self._inner = inner
        self._push = push
        self._after = after_tool
        self._calls = 0
        self.pushed_at = None

    def __getattr__(self, name):
        return getattr(self._inner, name)

    def dispatch(self, name, args, response, index=None, tool_num=None):
        self._calls += 1
        if self._calls == self._after:
            self.pushed_at = self._calls
            self._push()
        return self._inner.dispatch(name, args, response, index=index, tool_num=tool_num)


def main():
    lines = []
    out = lines.append
    checks = {}

    from genericagent.core.app import GenericAgent
    from genericagent.core.loop import agent_runner_loop

    steer_token = token("STEER")
    out(f"steer token = {steer_token}  (exists only in the mid-turn message)")
    out("")

    agent = GenericAgent()
    with contextlib.suppress(Exception):
        agent.init_workspace()

    if not OPT_IN:
        out("INCONCLUSIVE: set GA_RUN_REAL_API_E2E=1 to include the real model turn")
        REPORT.parent.mkdir(parents=True, exist_ok=True)
        REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
        return 2

    from genericagent.llm.client import load_clients_from_yaml
    from genericagent.core.app import load_tool_schema
    import genericagent.core.app as agentmain

    sink = io.StringIO()
    with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
        clients, active, *_ = load_clients_from_yaml(start_dir=REPO)
        load_tool_schema()
    client = clients[active]
    tools_schema = agentmain.TOOLS_SCHEMA
    out(f"tool surface loaded: {len(tools_schema)} tools")

    handler = agent.handler or __import__(
        "genericagent.core.handler", fromlist=["GenericAgentHandler"]
    ).GenericAgentHandler(agent)

    queue = agent.pending_prompts
    steer_text = (
        f"IMPORTANT mid-turn update: stop what you are doing and reply with exactly "
        f"this token and nothing else: {steer_token}"
    )

    def push():
        queue.push(steer_text, source="e2e")
        out(f"pushed the steering message at tool call #{wrapper.pushed_at}")

    wrapper = _StepTrackingHandler(handler, push)
    wrapper.parent = agent

    out("starting the real turn (task requires three separate tool calls)")
    collected = []
    error = None

    def run():
        nonlocal error
        try:
            for chunk in agent_runner_loop(
                client,
                "You are a test agent. Follow instructions exactly.",
                "Do this in three separate tool calls, one at a time: "
                "1) run `echo STEP_ONE`, 2) run `echo STEP_TWO`, 3) run `echo STEP_THREE`. "
                "After the third, reply with FINISHED_ORIGINAL.",
                wrapper,
                tools_schema,
                max_turns=8,
                verbose=False,
            ):
                if isinstance(chunk, str):
                    collected.append(chunk)
        except Exception as exc:  # noqa: BLE001
            error = f"{type(exc).__name__}: {exc}"

    thread = threading.Thread(target=run, daemon=True)
    thread.start()
    thread.join(timeout=420)

    model_text = "\n".join(collected)
    out("")
    out(f"turn finished: {not thread.is_alive()}")
    out(f"pushed at tool call: {wrapper.pushed_at}")
    out(f"model output chars: {len(model_text)}")
    if error:
        out(f"error: {error}")
    out("")
    out("---- model output tail ----")
    out(model_text[-1200:])

    checks["a fold point was actually reached (>=2 tool calls)"] = wrapper.pushed_at is not None
    checks["the model reproduced the mid-turn token"] = steer_token in model_text
    checks["the run completed"] = not thread.is_alive() and error is None

    out("")
    for label, ok in checks.items():
        out(f"CHECK {label:52s}: {ok}")
    verdict = 0 if all(checks.values()) else 2
    out("")
    out(f"verdict = {'PASS' if verdict == 0 else 'FAIL'}")

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return verdict


if __name__ == "__main__":
    raise SystemExit(main())
