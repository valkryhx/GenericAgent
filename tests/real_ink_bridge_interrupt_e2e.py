"""Real-API E2E for interrupting a turn from the UI path (Ink bridge), after the S12 watcher.

Why this exists: S12 added `GenericAgent._start_stop_file_watcher()` around the turn in
`agentmain.py`. That watcher is for the *subagent* path (`_stop` file, `verbose=False`), but the
code it wraps is the same `agent_runner_loop` block every frontend runs. The UI interrupts a
different way — Ink ESC → `{"type":"stop"}` → `ink_bridge.stop()` → `agent.abort()`, in-memory,
no `_stop` file — and `task_dir` is None there, so the watcher must return None and change
nothing. "Must" is a claim about running code, so it is measured here rather than reasoned about.

Driven through `GenericAgentBridge` itself (not a hand-rolled agent loop) so the JSONL event
contract the Ink UI consumes is what gets asserted: `status: running` → `stopping` → `idle`.

What it verifies end to end, on a real LLM turn:
  U1  ESC-equivalent `stop()` while the provider call is genuinely in flight ends the turn in
      seconds, emits `status: stopping`, and lands back at `status: idle`
  U2  the long answer did not finish, i.e. the interrupt cut it short
  U3  no `ga-stop-file-watcher` thread is ever created on the UI path (task_dir is None)
  U4  the agent is reusable afterwards: a second real turn completes and streams

Opt in with GA_RUN_REAL_API_E2E=1. Model defaults to the `cc-opus-5` profile (claude-opus-5).

Negative controls (temp probe patched `agentmain.py`, then reverted). Two mechanisms serve the UI
interrupt and either one alone is sufficient, so disabling one at a time does NOT go red:
  - `abort()`'s `cancel_current_request()` loop disabled → still passes, 0.094s → 5.234s
    (the chunk loop's `stop_sig` break catches it once the call returns)
  - the chunk loop's `if self.stop_sig: break` disabled → still passes, 0.093s
    (provider cancellation ends the call outright)
  - **both disabled → red**: `secondsToIdle: null`, status stuck at `stopping`, `stop_sig` left
    True, and the next submit refused as busy — 12685 streamed chars instead of 582.
That last run is what proves the assertions can fail. It also documents the redundancy: S12's
watcher is a third path, and on the UI it is inert by design (U3).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
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


EXPECTED_MODEL = os.environ.get("GA_REAL_API_EXPECTED_MODEL", "claude-opus-5")
EXPECTED_PROFILE = os.environ.get("GA_REAL_API_EXPECTED_NAME", "cc-opus-5")
OPT_IN = os.environ.get("GA_RUN_REAL_API_E2E") == "1"
SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_-]{12,}|Bearer\s+[A-Za-z0-9._~+/=-]{24,})", re.IGNORECASE)

STAMP = int(time.time())
PROMPT_PROBE_MARKER = f"GA_UI_LONGRUN_{STAMP}"
SHORT_MARKER = f"GA_UI_SECOND_TURN_{STAMP}"

# Same shape as the subagent E2E's long prompt: measured there to run past 300s un-interrupted,
# so any turn that ends in seconds ended because it was interrupted.
LONG_PROMPT = (
    f"[{PROMPT_PROBE_MARKER}] 不要调用任何工具。请从 1 数到 2000，每行输出一个数字、它的平方和它的立方，"
    "格式 `n: n*n n*n*n`。必须逐行完整输出，不要省略，不要总结，不要提前结束。"
)
LONG_PROMPT_FINAL_LINE = "2000: 4000000 8000000000"
SHORT_PROMPT = f"不要调用工具，不要输出 Markdown。只输出这一行精确文本：{SHORT_MARKER}"

WATCHER_THREAD_NAME = "ga-stop-file-watcher"

# The bridge's `_run_agent` thread holds `backend_output_redirect()` for the life of the process,
# so by the time this script prints, `sys.stdout` is the bridge backend log. Measured: the first
# run wrote a 0-byte report and the JSON turned up in temp/ink_bridge_backend.log. Keep the real
# stdout from import time and report on that.
REAL_STDOUT = sys.stdout


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


def watcher_threads() -> list[str]:
    return [t.name for t in threading.enumerate() if WATCHER_THREAD_NAME in t.name]


def provider_request_sent(log_path: str, marker: str) -> bool:
    """Is the provider call actually in flight?

    `llmcore._write_llm_log('Prompt', ...)` runs on the line before `backend.ask(...)`, and each
    agent gets its own `temp/model_responses/model_responses_*.txt` via `self.log_path`. Measured
    in the subagent E2E: weaker gates (streamed bytes, output file existence, turn_status) all
    fire before the request goes out or depend on when the model flushes.
    """
    try:
        return marker in Path(log_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return False


def resolve_llm_no(agent) -> tuple[int, dict]:
    for idx, client in enumerate(getattr(agent, "llmclients", []) or []):
        backend = getattr(client, "backend", None)
        name, model = getattr(backend, "name", ""), getattr(backend, "model", "")
        if model == EXPECTED_MODEL and (name == EXPECTED_PROFILE or EXPECTED_PROFILE in {"", "*"}):
            return idx, {"index": idx, "name": name, "model": model}
    raise RuntimeError(f"profile not found for model={EXPECTED_MODEL} name={EXPECTED_PROFILE}")


def build_bridge(events: list[dict], summary: dict):
    """A real `GenericAgent` behind a real `GenericAgentBridge`, pinned to the target profile.

    `default_agent_factory` is not used because the profile has to be selected before the first
    turn; `select_llm` is the same call the UI's `/model` command makes.
    """
    from ink_bridge import GenericAgentBridge

    captured = io.StringIO()
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        from agentmain import GenericAgent

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


def statuses(events: list[dict]) -> list[str]:
    return [str(e.get("status")) for e in events if e.get("type") == "status"]


def assistant_text(events: list[dict], task_id: int) -> str:
    parts = []
    for event in events:
        if event.get("taskId") != task_id:
            continue
        if event.get("type") == "assistant_delta":
            parts.append(str(event.get("text") or ""))
        elif event.get("type") == "assistant_done":
            parts.append(str(event.get("text") or ""))
    return "".join(parts)


def check_ui_interrupt(bridge, agent, events: list[dict], summary: dict) -> None:
    """U1/U2/U3: ESC-equivalent stop during a real provider call."""
    row: dict[str, Any] = {}
    summary["uiInterrupt"] = row
    row["taskDirIsNone"] = getattr(agent, "task_dir", "unset") is None
    if not row["taskDirIsNone"]:
        # The whole point of this script: on the UI path there is no task dir, so the S12
        # watcher must not start. If a frontend ever sets one, this assertion is the warning.
        summary["issues"].append("ui_agent_unexpectedly_has_a_task_dir")
    row["watcherThreadsBeforeSubmit"] = watcher_threads()

    task_id = bridge.submit(LONG_PROMPT)
    row["taskId"] = task_id
    if task_id < 0:
        summary["issues"].append("bridge_refused_the_submit")
        return
    row["statusAfterSubmit"] = statuses(events)[-1] if statuses(events) else None

    in_flight = wait_for(
        lambda: provider_request_sent(agent.log_path, PROMPT_PROBE_MARKER), timeout_s=180, interval_s=0.2
    )
    row["providerRequestLogged"] = bool(in_flight)
    if not in_flight:
        summary["issues"].append("provider_request_never_went_out")
        return
    # Measured in the subagent E2E's negative control: interrupting before the request is on the
    # wire proves nothing, because the pre-fix chunk-loop check would have caught it anyway.
    row["agentIsRunningAtStop"] = bool(getattr(agent, "is_running", False))
    if not row["agentIsRunningAtStop"]:
        summary["issues"].append("turn_already_finished_before_stop_was_sent")
    row["watcherThreadsDuringTurn"] = watcher_threads()
    if row["watcherThreadsDuringTurn"]:
        # Not a correctness failure by itself, but it would mean the UI path grew a task_dir
        # and the watcher is now polling a file nobody writes.
        summary["issues"].append("stop_file_watcher_started_on_the_ui_path")

    sent_at = time.monotonic()
    bridge.stop()
    row["statusRightAfterStop"] = statuses(events)[-1] if statuses(events) else None
    if row["statusRightAfterStop"] != "stopping":
        summary["issues"].append("stop_did_not_emit_the_stopping_status_the_ui_renders")

    idle = wait_for(lambda: statuses(events)[-1:] == ["idle"], timeout_s=60, interval_s=0.1)
    row["secondsToIdle"] = None if not idle else round(time.monotonic() - sent_at, 3)
    row["reachedIdle"] = bool(idle)
    if not idle:
        summary["issues"].append("ui_never_returned_to_idle_after_stop")
    # The un-interrupted version of this prompt does not finish inside 300s (measured: the
    # subagent path times out on dq.get(timeout=300)), so seconds here means the stop landed
    # mid-call rather than at the natural end of the turn.
    if row["secondsToIdle"] is not None and row["secondsToIdle"] > 30:
        summary["issues"].append("stop_latency_looks_like_turn_boundary_not_midturn")

    text = assistant_text(events, task_id)
    row["assistantChars"] = len(text)
    row["reachedFinalLine"] = LONG_PROMPT_FINAL_LINE in text
    if row["reachedFinalLine"]:
        summary["issues"].append("stop_did_not_cut_the_answer_short")
    row["sawAssistantDone"] = any(
        e.get("type") == "assistant_done" and e.get("taskId") == task_id for e in events
    )
    row["statusSequence"] = statuses(events)
    row["agentIsRunningAfterStop"] = bool(getattr(agent, "is_running", False))
    if row["agentIsRunningAfterStop"]:
        summary["issues"].append("agent_still_marked_running_after_stop")
    row["stopSigCleared"] = getattr(agent, "stop_sig", None) is False
    if not row["stopSigCleared"]:
        # A stop_sig left True would make the *next* turn break out immediately — the failure
        # mode a user sees as "the UI is dead after I pressed ESC once".
        summary["issues"].append("stop_sig_left_set_which_would_break_the_next_turn")
    row["watcherThreadsAfterStop"] = watcher_threads()


def check_reusable_after_interrupt(bridge, agent, events: list[dict], summary: dict) -> None:
    """U4: the UI is still usable after ESC — a second real turn completes and streams.

    This is the regression that matters most for "did S12 break anything": the watcher's
    `finally: stop_watcher()` and the existing `self.is_running = self.stop_sig = False` both run
    on the aborted turn, and if either left state behind the next submit would be refused as busy
    or would break out of the chunk loop immediately.
    """
    row: dict[str, Any] = {}
    summary["reuseAfterInterrupt"] = row

    accepted = wait_for(lambda: not getattr(agent, "is_running", False), timeout_s=30, interval_s=0.2)
    row["agentIdleBeforeSecondSubmit"] = bool(accepted)
    task_id = bridge.submit(SHORT_PROMPT)
    row["taskId"] = task_id
    if task_id < 0:
        summary["issues"].append("second_submit_was_refused_after_the_interrupt")
        return

    done = wait_for(
        lambda: any(
            e.get("type") == "assistant_done" and e.get("taskId") == task_id for e in events
        ),
        timeout_s=300,
        interval_s=0.5,
    )
    row["secondTurnCompleted"] = bool(done)
    if not done:
        summary["issues"].append("second_real_turn_never_completed_after_the_interrupt")
        return

    text = assistant_text(events, task_id)
    row["markerEchoed"] = SHORT_MARKER in text
    if not row["markerEchoed"]:
        summary["issues"].append("second_turn_did_not_produce_the_requested_marker")
    row["deltaEvents"] = sum(
        1 for e in events if e.get("type") == "assistant_delta" and e.get("taskId") == task_id
    )
    if not row["deltaEvents"]:
        # inc_out=True means the UI expects incremental deltas; a done-only turn would render
        # as a frozen screen followed by a wall of text.
        summary["issues"].append("second_turn_streamed_nothing_incrementally")
    row["watcherThreadsAfterSecondTurn"] = watcher_threads()
    if row["watcherThreadsAfterSecondTurn"]:
        summary["issues"].append("stop_file_watcher_leaked_a_thread")
    row["finalStatus"] = statuses(events)[-1] if statuses(events) else None
    if row["finalStatus"] != "idle":
        summary["issues"].append("ui_did_not_settle_at_idle_after_the_second_turn")


def main() -> int:
    summary: dict[str, Any] = {
        "passed": False,
        "skipped": False,
        "expectedModel": EXPECTED_MODEL,
        "expectedProfile": EXPECTED_PROFILE,
        "issues": [],
    }
    if not OPT_IN:
        summary.update({"skipped": True, "reason": "set GA_RUN_REAL_API_E2E=1 to run the UI interrupt E2E"})
        report(summary)
        return 0

    bridge = None
    events: list[dict] = []
    try:
        bridge, agent = build_bridge(events, summary)
        check_ui_interrupt(bridge, agent, events, summary)
        check_reusable_after_interrupt(bridge, agent, events, summary)
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
            with contextlib.suppress(Exception):
                bridge.stop()


if __name__ == "__main__":
    raise SystemExit(main())


