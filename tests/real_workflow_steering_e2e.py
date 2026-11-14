"""Real-API E2E: does steering reach the agent *while a workflow is running*?

The claim to verify is narrow and testable: when a dynamic workflow is in flight,
a message the user types must reach the model promptly -- either folded into the
turn that launched it, or as a new turn if the launcher turn already ended.
"Prompty" means bounded by a fold point, not by the workflow's own duration.

Why this is not obvious from the code, and therefore worth measuring:

  * a workflow runs on its own bridge thread (``_run_workflow_runtime``) and the
    main agent's turn ends right after ``do_workflow`` returns ``async_launched``
    with an instruction to end the turn. So the *usual* state during a workflow is
    "main agent idle", which means the natural path is ``submit`` (a new turn), not
    ``queue_prompt``. Asserting only the queue would test a path this scenario does
    not take.
  * if the model ignores that instruction and blocks in ``wait_agent``, the queue
    path is the one that matters, and it is bounded by ``WAIT_SLICE_MAX_S`` (10s)
    rather than by the requested timeout. That bound is the interesting number.
  * the workflow completion handoff (``_queue_workflow_handoff``) starts its own
    turn. If a steering turn is running at that moment the two can collide, and a
    collision is exactly what "steering during a workflow" would look like from the
    user's side: their message vanishes or is answered twice.

What this script does NOT do: it does not read a fixed expected answer. Each
steering message carries a random marker that exists nowhere else, and the verdict
is whether the model reproduced it -- the only evidence that the text arrived.

Run (profile names vary per environment; ``default`` on this machine):

    $env:GA_RUN_REAL_API_E2E="1"
    $env:GA_REAL_API_EXPECTED_NAME="default"
    python tests/real_workflow_steering_e2e.py
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import random
import re
import string
import sys
import threading
import time
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[1]
FRONTENDS = REPO / "frontends"
for entry in (REPO, FRONTENDS):
    if str(entry) not in sys.path:
        sys.path.insert(0, str(entry))

EXPECTED_MODEL = os.environ.get("GA_REAL_API_EXPECTED_MODEL", "moma/deepseek-latest-flash")
EXPECTED_PROFILE = os.environ.get("GA_REAL_API_EXPECTED_NAME", "default")
OPT_IN = os.environ.get("GA_RUN_REAL_API_E2E") == "1"
SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_-]{12,}|Bearer\s+[A-Za-z0-9._~+/=-]{24,})", re.IGNORECASE)

STAMP = int(time.time())

REAL_STDOUT = sys.stdout


def marker(prefix: str) -> str:
    alphabet = string.ascii_uppercase + string.digits
    return prefix + "".join(random.choice(alphabet) for _ in range(10))


def report(payload: dict) -> None:
    print(json.dumps(sanitize(payload), ensure_ascii=False, indent=2), file=REAL_STDOUT, flush=True)


def sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): sanitize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if not isinstance(value, str):
        return value
    return SECRET_RE.sub("[REDACTED_SECRET]", value)


def wait_for(predicate, timeout_s: float, interval_s: float = 0.2):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval_s)
    return None


# ---------------------------------------------------------------------------
# The workflow. Two children, each slow enough that the run is still in flight
# when the steering message is typed. `sleep` is deliberately not used: the
# point is to keep real child turns alive, so the parent has a genuine
# background run to steer against.
# ---------------------------------------------------------------------------

WORKFLOW_SCRIPT = r'''
export const meta = {
  name: 'steering-during-workflow',
  description: 'Slow two-child workflow used to prove steering lands mid-run',
}
phase('Slow work')
const first = await agent(
  `Do exactly this, with no extra commentary: use code_run to run ` +
  `\`python -c "import time; time.sleep(60); print('A_DONE')"\`, then reply with the single word DONE.`,
  { label: 'first-slow-child' }
)
phase('Slow work again')
const second = await agent(
  `Do exactly this, with no extra commentary: use code_run to run ` +
  `\`python -c "import time; time.sleep(60); print('B_DONE')"\`, then reply with the single word DONE.`,
  { label: 'second-slow-child' }
)
return { marker: 'GA_WORKFLOW_STEER_DONE', first: String(first || '').slice(0, 40), second: String(second || '').slice(0, 40) }
'''


def resolve_llm_no(agent):
    for idx, client in enumerate(getattr(agent, "llmclients", []) or []):
        backend = getattr(client, "backend", None)
        name, model = getattr(backend, "name", ""), getattr(backend, "model", "")
        if model == EXPECTED_MODEL and (EXPECTED_PROFILE in {"", "*"} or name == EXPECTED_PROFILE):
            return idx, {"index": idx, "name": name, "model": model}
    raise RuntimeError(f"profile not found for model={EXPECTED_MODEL} name={EXPECTED_PROFILE}")


def build_bridge(events, summary):
    from frontends.ink.bridge import GenericAgentBridge

    captured = io.StringIO()
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        from genericagent.core.app import GenericAgent

        agent = GenericAgent()
        agent.inc_out = True
        agent.verbose = True
        llm_no, profile_row = resolve_llm_no(agent)
        switched = agent.select_llm(str(llm_no))
    summary["profile"] = profile_row
    summary["modelSwitchOk"] = bool(switched.get("ok"))
    if not switched.get("ok"):
        summary["issues"].append("could_not_select_the_target_profile")
    bridge = GenericAgentBridge(agent_factory=lambda: agent, emit=events.append)
    return bridge, agent


def statuses(events):
    return [str(e.get("status")) for e in events if e.get("type") == "status"]


def running_workflow_run_ids(bridge, events):
    """Run ids the bridge considers live, straight from its workflow threads."""
    live = []
    for run_id, thread in list(getattr(bridge, "_workflow_threads", {}).items()):
        if thread is not None and thread.is_alive():
            live.append(run_id)
    return live


def start_workflow(bridge, summary):
    """Launch through the real bridge entry point the model's tool uses.

    ``workflow_launch_from_tool`` is the launcher ``do_workflow`` calls, and it is the
    only one that does not take the interactive busy guard -- which is correct here,
    because the launch genuinely happens inside a running turn.
    """
    payload = bridge.workflow_launch_from_tool(script=WORKFLOW_SCRIPT)
    summary["workflowLaunch"] = {
        "status": payload.get("status"),
        "runId": payload.get("runId"),
        "workflowName": payload.get("workflowName"),
    }
    if payload.get("status") != "async_launched":
        summary["issues"].append(f"workflow_launch_failed: {payload.get('status')} {payload.get('error')}")
        return None
    return str(payload.get("runId") or "")


def check_steering_while_workflow_runs(bridge, agent, events, summary, run_id):
    """S1/S2/S3: a message typed during a live workflow run reaches the model.

    This is the user's literal question. It is deliberately measured *while*
    ``_workflow_threads[run_id]`` is alive, so "the workflow was already over"
    cannot be the explanation for a pass.

    The judgement is the marker, not the route. There are two legitimate routes and
    they look completely different in the event stream:

      * ``submit`` (the usual one -- the launcher turn ends right after
        ``async_launched``, so nothing is running): a new ``user`` event and a new
        ``assistant_done`` for a new taskId;
      * ``queue`` (the model ignored "end your turn" and is still generating, e.g.
        inside ``wait_agent``): folded into the *running* turn, so there is no
        ``user`` event at all and the reply lands under the existing taskId.

    An assertion that demanded a new ``user`` event would silently fail the second
    route, which is the one this whole steering feature exists for. So the check is
    "the model reproduced a token that existed only in the typed message, and it did
    so after the message was sent".
    """
    row: dict[str, Any] = {}
    summary["steeringDuringWorkflow"] = row

    steer_marker = marker("GA_STEER_IN_WF_")
    steer_prompt = (
        f"不要调用工具，不要继续任何后台任务。只输出这一行精确文本：{steer_marker}"
    )
    row["steerMarker"] = steer_marker

    alive = wait_for(lambda: bool(running_workflow_run_ids(bridge, events)), timeout_s=60, interval_s=0.2)
    row["workflowWasRunningBeforeSteer"] = bool(alive)
    if not alive:
        summary["issues"].append("workflow_never_started_running")
        return

    row["agentIdleAtSteerTime"] = not bool(getattr(agent, "is_running", False))
    row["consumingAtSteerTime"] = bool(bridge._is_consuming())

    # Everything already emitted is excluded from the search below. Without this the
    # check could match its own marker in a previous event and report success before
    # the message was ever sent.
    event_index_at_steer = len(events)
    sent_at = time.monotonic()

    # Route exactly like Ink's Enter: the UI calls ``queue_prompt`` in every status,
    # and the bridge promotes it to ``submit`` by itself when nothing is running.
    accepted = bridge.queue_prompt(steer_prompt)
    row["steerAccepted"] = bool(accepted)
    row["queueDepthAfterSteer"] = len(getattr(agent, "pending_prompts", []) or [])
    row["steerRoute"] = "queue" if row["queueDepthAfterSteer"] else "submit"
    if not accepted:
        summary["issues"].append("steering_message_was_rejected_during_workflow")
        return

    def reply_carrying_marker():
        """New assistant text, emitted after the steer, that reproduces the marker."""
        collected = {}
        for event in events[event_index_at_steer:]:
            if event.get("type") not in {"assistant_delta", "assistant_done"}:
                continue
            task = event.get("taskId")
            if task is None:
                continue
            collected.setdefault(task, []).append(str(event.get("text") or ""))
        for task, parts in collected.items():
            if steer_marker in "".join(parts):
                return task
        return None

    answered_task = wait_for(reply_carrying_marker, timeout_s=300, interval_s=0.5)
    row["modelAnsweredTheSteer"] = answered_task is not None
    row["secondsToModelAnswer"] = (
        None if answered_task is None else round(time.monotonic() - sent_at, 3)
    )
    if answered_task is None:
        summary["issues"].append("model_never_reproduced_the_steering_marker")
        return
    row["answeredTaskId"] = answered_task

    # Which route it actually took, read from the evidence rather than assumed.
    new_user_events = [
        e for e in events[event_index_at_steer:]
        if e.get("type") == "user" and steer_marker in str(e.get("text") or "")
    ]
    row["steerBecameItsOwnTurn"] = bool(new_user_events)
    row["observedRoute"] = "submit" if new_user_events else "queue_folded_into_running_turn"
    if new_user_events and int(new_user_events[0].get("taskId") or 0) != answered_task:
        summary["issues"].append("steering_turn_id_and_answering_turn_id_disagree")

    # Only now is "during the workflow" a meaningful claim about the answer itself.
    row["workflowStillRunningWhenAnswered"] = bool(running_workflow_run_ids(bridge, events))
    if not row["workflowStillRunningWhenAnswered"]:
        summary["issues"].append("workflow_finished_before_the_steering_was_answered")



def check_steering_when_agent_is_busy_during_workflow(bridge, agent, events, summary):
    """S5: the hard route -- a workflow is live AND the main agent is generating.

    The previous check runs the common case: the launcher turn ends immediately, so
    the agent is idle and the message takes the ``submit`` route. That is the easy
    path -- a new turn starting on an idle agent has nothing to go wrong.

    This one forces the route the user's original complaint was about: the model
    ignores "end your turn" and blocks in ``wait_agent`` while the workflow runs.
    Then the only way a message can arrive is the fold point, and the only thing
    bounding the delay is ``Manager.WAIT_SLICE_MAX_S`` (10s) -- not the timeout the
    model asked for. If a steering message can take minutes here, this is where it
    shows.

    A pass requires all three: the message is accepted, the model reproduces its
    marker *in the running turn's own stream*, and the workflow was still alive
    throughout.
    """
    row: dict[str, Any] = {}
    summary["steeringWhileBusy"] = row

    # A prompt that keeps the turn open on purpose: launch a workflow with the real
    # tool, then poll it. The model is told to keep waiting rather than end, which is
    # exactly the behaviour observed in the real session that motivated this work.
    holder_prompt = (
        "用 workflow 工具启动一个后台工作流，脚本内容如下（原样传 script 参数）：\n\n"
        + WORKFLOW_SCRIPT
        + "\n\n启动后立刻调用 wait_agent 轮询这个 workflow（targets 传返回的 runId，"
        "timeout_seconds 给 300），并重复轮询直到它结束。轮询期间不要结束回合，"
        "不要输出最终答案，每次 wait_agent 返回后继续再等。全程只做这一件事。"
    )
    holder_task = bridge.submit(holder_prompt)
    row["holderTaskId"] = holder_task
    if holder_task < 0:
        summary["issues"].append("busy_scenario_submit_refused")
        return

    # Wait until the agent is genuinely generating AND a workflow is live. Both are
    # required: a workflow alone would fall back to the easy path, and a busy agent
    # alone would not be "during a workflow".
    both = wait_for(
        lambda: bool(getattr(agent, "is_running", False)) and bool(running_workflow_run_ids(bridge, events)),
        timeout_s=240,
        interval_s=0.2,
    )
    row["agentBusyAndWorkflowLive"] = bool(both)
    if not both:
        row["agentWasRunning"] = bool(getattr(agent, "is_running", False))
        row["workflowThreadsAlive"] = running_workflow_run_ids(bridge, events)
        summary["issues"].append("could_not_reach_busy_agent_with_live_workflow")
        return

    steer_marker = marker("GA_STEER_BUSY_")
    steer_prompt = f"不要调用工具。只输出这一行精确文本：{steer_marker}"
    row["steerMarker"] = steer_marker

    event_index_at_steer = len(events)
    sent_at = time.monotonic()
    accepted = bridge.queue_prompt(steer_prompt)
    row["steerAccepted"] = bool(accepted)
    row["queueDepthAfterSteer"] = len(getattr(agent, "pending_prompts", []) or [])
    row["steerRoute"] = "queue" if row["queueDepthAfterSteer"] else "submit"
    if not accepted:
        summary["issues"].append("busy_steer_rejected")
        return
    if row["steerRoute"] != "queue":
        # If the agent went idle in the window between the wait and the push, the test
        # no longer measures the fold path. Say so instead of reporting a pass that
        # proved something easier.
        summary["issues"].append("busy_steer_did_not_take_the_queue_route")
        return

    def reply_carrying_marker():
        collected = {}
        for event in events[event_index_at_steer:]:
            if event.get("type") not in {"assistant_delta", "assistant_done"}:
                continue
            task = event.get("taskId")
            if task is None:
                continue
            collected.setdefault(task, []).append(str(event.get("text") or ""))
        for task, parts in collected.items():
            if steer_marker in "".join(parts):
                return task
        return None

    answered_task = wait_for(reply_carrying_marker, timeout_s=300, interval_s=0.5)
    row["modelAnsweredTheSteer"] = answered_task is not None
    row["secondsToModelAnswer"] = (
        None if answered_task is None else round(time.monotonic() - sent_at, 3)
    )
    if answered_task is None:
        summary["issues"].append("busy_steer_never_reached_the_model")
        return
    row["answeredTaskId"] = answered_task
    # Folded into the running turn: no new `user` event, same task id as the holder.
    row["foldedIntoRunningTurn"] = answered_task == holder_task
    if answered_task != holder_task:
        summary["issues"].append("busy_steer_did_not_land_in_the_running_turn")
    row["queueEmptiedAfterDelivery"] = len(getattr(agent, "pending_prompts", []) or []) == 0
    if not row["queueEmptiedAfterDelivery"]:
        # A message handed to the model but left in the queue is delivered twice.
        summary["issues"].append("busy_steer_left_the_queue_non_empty")
    row["workflowStillRunningWhenAnswered"] = bool(running_workflow_run_ids(bridge, events))

    # Latency is the whole question ("及时"). A fold that only lands after the model's
    # 300s wait would still technically "work" and be useless to a user.
    row["withinTwiceTheWaitSlice"] = (
        row["secondsToModelAnswer"] is not None and row["secondsToModelAnswer"] <= 25.0
    )
    if not row["withinTwiceTheWaitSlice"]:
        summary["issues"].append(
            f"busy_steer_took_{row['secondsToModelAnswer']}s_exceeding_two_wait_slices"
        )

    wait_for(lambda: not getattr(agent, "is_running", False), timeout_s=600, interval_s=0.5)


def check_workflow_completion_still_hands_off(bridge, agent, events, summary, run_id):
    """S4: steering did not break the workflow's own completion handoff.

    The failure this guards against is the one a user would notice last and trust
    least: the steering turn works, and the workflow's result is silently dropped
    because two turns raced over ``_consume_thread``.
    """
    row: dict[str, Any] = {}
    summary["workflowHandoffAfterSteering"] = row

    handed = wait_for(
        lambda: run_id in getattr(bridge, "_workflow_handed_off", set()),
        timeout_s=900,
        interval_s=0.5,
    )
    row["handoffQueued"] = bool(handed)
    if not handed:
        summary["issues"].append("workflow_never_handed_off_after_steering")
        return

    final_seen = wait_for(
        lambda: [e for e in events if e.get("type") == "workflow_final" and e.get("runId") == run_id],
        timeout_s=120,
        interval_s=0.2,
    )
    row["workflowFinalEmitted"] = bool(final_seen)
    if not final_seen:
        summary["issues"].append("workflow_final_event_missing")
        return
    result = final_seen[0].get("result") or {}
    row["finalStatus"] = result.get("status")
    row["finalMarkerPresent"] = "GA_WORKFLOW_STEER_DONE" in json.dumps(result, ensure_ascii=False)
    if not row["finalMarkerPresent"]:
        summary["issues"].append("workflow_result_did_not_carry_its_own_marker")

    # The handoff turn: ``_queue_workflow_handoff`` does not emit a ``user`` event (it
    # calls ``put_task`` and emits only ``status: running``), so looking for one was a
    # vacuous check. Read the evidence the bridge actually produces: a *new* task id
    # that starts running after this workflow's ``workflow_final``.
    handoff_runs = [
        int(e.get("taskId") or 0)
        for index, e in enumerate(events)
        if e.get("type") == "status"
        and e.get("status") == "running"
        and int(e.get("taskId") or 0) > int(summary.get("steeringDuringWorkflow", {}).get("answeredTaskId") or 0)
        and any(
            earlier.get("type") == "workflow_final" and earlier.get("runId") == run_id
            for earlier in events[:index]
        )
    ]
    row["handoffTurnTaskIds"] = handoff_runs
    row["handoffTurnObserved"] = bool(handoff_runs)
    if not handoff_runs:
        summary["issues"].append("workflow_handoff_turn_never_started_after_steering")
    row["taskIdsWithAssistantDone"] = sorted({
        int(e.get("taskId") or 0)
        for e in events
        if e.get("type") == "assistant_done" and e.get("taskId") is not None
    })
    idle = wait_for(lambda: statuses(events)[-1:] == ["idle"], timeout_s=300, interval_s=0.5)
    row["settledAtIdle"] = bool(idle)
    if not idle:
        summary["issues"].append("bridge_never_settled_at_idle_after_handoff")
    wait_for(lambda: not getattr(agent, "is_running", False), timeout_s=30, interval_s=0.1)
    row["agentIdleAtEnd"] = not bool(getattr(agent, "is_running", False))


def main() -> int:
    summary: dict[str, Any] = {
        "passed": False,
        "skipped": False,
        "expectedModel": EXPECTED_MODEL,
        "expectedProfile": EXPECTED_PROFILE,
        "stamp": STAMP,
        "issues": [],
    }
    if not OPT_IN:
        summary.update({"skipped": True, "reason": "set GA_RUN_REAL_API_E2E=1 to run the workflow steering E2E"})
        report(summary)
        return 0

    bridge = None
    events: list[dict] = []
    try:
        # Negative control: raise the wait slice and the busy-path latency must grow with
        # it. Without this knob the 25s bound in ``check_steering_when_agent_is_busy``
        # could pass by luck of one fast run; with it, the assertion is shown to be
        # measuring the fold point rather than wall-clock noise.
        #
        #   $env:GA_E2E_WAIT_SLICE_MAX_S="120"   -> expect the busy steer to take ~120s
        slice_override = os.environ.get("GA_E2E_WAIT_SLICE_MAX_S")
        if slice_override:
            from genericagent.agents.manager import SubagentManager

            SubagentManager.WAIT_SLICE_MAX_S = float(slice_override)
            summary["waitSliceMaxS"] = float(slice_override)
        bridge, agent = build_bridge(events, summary)
        run_id = start_workflow(bridge, summary)
        if run_id:
            check_steering_while_workflow_runs(bridge, agent, events, summary, run_id)
            check_workflow_completion_still_hands_off(bridge, agent, events, summary, run_id)
            # Runs last: it deliberately holds the agent busy for minutes, so everything
            # that needs a quiet agent has to finish first.
            check_steering_when_agent_is_busy_during_workflow(bridge, agent, events, summary)
        summary["runId"] = run_id
        summary["eventTypes"] = sorted({str(e.get("type")) for e in events})
        summary["passed"] = not summary["issues"]
        report(summary)
        return 0 if summary["passed"] else 2
    except Exception as exc:
        summary["error"] = sanitize(f"{type(exc).__name__}: {exc}")
        if "exception" not in summary["issues"]:
            summary["issues"].append("exception")
        report(summary)
        return 1
    finally:
        if bridge is not None:
            for run_id, thread in list(getattr(bridge, "_workflow_threads", {}).items()):
                with contextlib.suppress(Exception):
                    bridge.workflow_stop(run_id)
            with contextlib.suppress(Exception):
                bridge.stop()


if __name__ == "__main__":
    raise SystemExit(main())
