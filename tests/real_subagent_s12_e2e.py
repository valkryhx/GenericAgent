"""Real-API E2E for the two S12 fixes, against a real child process and a real LLM turn.

Both fixes were driven by unit tests that stand in a fake client for the provider call.
That is exactly the part worth distrusting here: the interrupt fix exists because a *real*
long call yields nothing for seconds, and the malformed-file fix exists because a *real*
child dies before writing any state. So this script uses a real subagent process.

What it verifies end to end:
  I1  `interrupt_agent` during a genuinely running LLM turn ends that turn in seconds, not at
      the natural turn boundary minutes later
  I2  the interrupted child stops instead of finishing the long answer it was asked for, and
      stays alive for a followup — `interrupt_agent` is not `close_agent`
  H1  a corrupt `_history.json` handed to a real child does not kill it: the turn still
      completes, the bad bytes are quarantined, and history_load_failed is recorded
  H2  a corrupt `state.json` is copied aside before the child's first write replaces it

Opt in with GA_RUN_REAL_API_E2E=1. Model defaults to the `cc-opus-5` profile (claude-opus-5).

Negative control (temp/_probe_s12_negative.py, run with `stop_watcher = None` restoring the
pre-fix behaviour): I1 must go red, and it does — `secondsToTurnEnd: null`, turn_status still
`running` after 60s, vs 0.235s with the watcher in place. An earlier version of this script
passed that control, which is how the `turn_status == running` gate was found to be too early.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


EXPECTED_MODEL = os.environ.get("GA_REAL_API_EXPECTED_MODEL", "claude-opus-5")
EXPECTED_PROFILE = os.environ.get("GA_REAL_API_EXPECTED_NAME", "cc-opus-5")
OPT_IN = os.environ.get("GA_RUN_REAL_API_E2E") == "1"
SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_-]{12,}|Bearer\s+[A-Za-z0-9._~+/=-]{24,})", re.IGNORECASE)

STAMP = int(time.time())
INTERRUPT_TASK = f"s12_interrupt_{STAMP}"
HISTORY_TASK = f"s12_history_{STAMP}"
STATE_TASK = f"s12_state_{STAMP}"

HISTORY_MARKER = "GA_S12_HISTORY_OK_20260803"
STATE_MARKER = "GA_S12_STATE_OK_20260803"

# Long enough that the turn cannot finish before the interrupt lands. Measured: a 400-line
# request completed in ~25s on this profile, so the first run interrupted an already-finished
# turn (reached400: true, no agent_shutdown) and proved nothing. Measured on the 2000-line
# version: an *un-interrupted* turn never yields a chunk and dies on `dq.get(timeout=300)`
# (`agent_error`, `Empty @ queue.py:179`), so 300s+ is the natural turn length to beat.
# The ASCII marker is what makes the child's provider request findable in the LLM log — the
# Chinese text is written through a cp936 console and comes back mojibake.
PROMPT_PROBE_MARKER = f"GA_S12_LONGRUN_{STAMP}"
LONG_PROMPT = (
    f"[{PROMPT_PROBE_MARKER}] 不要调用任何工具。请从 1 数到 2000，每行输出一个数字、它的平方和它的立方，"
    "格式 `n: n*n n*n*n`。必须逐行完整输出，不要省略，不要总结，不要提前结束。"
)
LONG_PROMPT_FINAL_LINE = "2000: 4000000 8000000000"
SHORT_PROMPT = "不要调用工具，不要输出 Markdown。只输出这一行精确文本：{marker}"

CORRUPT_HISTORY = '[{"role": "user", "content": "truncated on purpose'


def sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): sanitize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if not isinstance(value, str):
        return value
    return SECRET_RE.sub("[REDACTED_SECRET]", value)


def resolve_profile() -> tuple[int, dict]:
    from llm_client import load_clients_from_yaml

    captured = io.StringIO()
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        clients, _active, _cfg_path, _mtime = load_clients_from_yaml(start_dir=REPO)
    for idx, client in enumerate(clients):
        backend = getattr(client, "backend", None)
        name, model = getattr(backend, "name", ""), getattr(backend, "model", "")
        if model == EXPECTED_MODEL and (name == EXPECTED_PROFILE or EXPECTED_PROFILE in {"", "*"}):
            return idx, {"index": idx, "name": name, "model": model}
    raise RuntimeError(f"profile not found for model={EXPECTED_MODEL} name={EXPECTED_PROFILE}")


def wait_for(predicate, timeout_s: float, interval_s: float = 0.2):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval_s)
    return None


def read_state(task_dir: Path) -> dict:
    try:
        return json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def event_types(task_dir: Path) -> list[str]:
    path = task_dir / "events.jsonl"
    if not path.exists():
        return []
    types = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            types.append(json.loads(line).get("type"))
        except Exception:
            continue
    return types


def events_of(task_dir: Path, wanted: str) -> list[dict]:
    path = task_dir / "events.jsonl"
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except Exception:
            continue
        if row.get("type") == wanted:
            rows.append(row)
    return rows


def output_text(task_dir: Path) -> str:
    parts = []
    for path in sorted(task_dir.glob("output*.txt")):
        try:
            parts.append(path.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            continue
    return "\n".join(parts)


def provider_request_sent(marker: str) -> str | None:
    """Has the child's provider request actually gone out on the wire?

    This is the only gate that distinguishes the fix from the bug, and finding that out cost a
    negative control. `turn_status == "running"` is written *before* `agent.put_task`, and
    `agent_loop` yields its "Turn 1 ..." banner before calling `client.chat`, so a `_stop` written
    at `running` is still sitting there when the pre-fix `for chunk in gen` check runs — measured:
    with the watcher stubbed out, the turn still ended in 7.89s and the phase passed.

    `llmcore._write_llm_log('Prompt', ...)` runs on the line before `backend.ask(...)`, so its
    appearance means the loop is now inside `exhaust(response_gen)` with no chunk pending — the
    exact window the watcher exists for. Each child writes its own
    `temp/model_responses/model_responses_*.txt` (agentmain sets `self.log_path` per agent).
    """
    log_dir = REPO / "temp" / "model_responses"
    if not log_dir.is_dir():
        return None
    for path in log_dir.glob("model_responses_*.txt"):
        try:
            if marker in path.read_text(encoding="utf-8", errors="replace"):
                return str(path)
        except OSError:
            continue
    return None


def _instrumented_manager(manager, spawned: list[str]):
    """Track spawned names and allow corrupting a control file just before the child starts.

    The corruption has to land after the manager has written `state.json` / `_history.json`
    and before `Popen`, which is exactly the seam a real parent-crash-mid-write would hit.
    Wrapping `manager.popen` gets that ordering without adding test-only parameters to
    production code.
    """
    import subprocess

    pending: dict[str, Any] = {"corrupt": None}

    def popen(cmd, **kwargs):
        corrupt = pending.pop("corrupt", None)
        pending["corrupt"] = None
        if corrupt:
            # Resolved here, not at spawn time: the manager writes state.json / _history.json
            # during spawn_agent, so a callback evaluated earlier would read a file that does
            # not exist yet (measured: FileNotFoundError on state.json).
            path, payload = corrupt()
            Path(path).write_text(payload, encoding="utf-8")
        return subprocess.Popen(cmd, **kwargs)

    manager.popen = popen

    def spawn(task_name, message, *, corrupt_file=None, **kwargs):
        if corrupt_file is not None:
            task_dir = manager.temp_dir / task_name
            pending["corrupt"] = lambda: corrupt_file(task_dir)
        handle = manager.spawn_agent(task_name, message, **kwargs)
        spawned.append(handle.task_name)
        return handle

    manager.spawn_tracked = spawn
    return manager


def check_interrupt(manager, llm_no: int, summary: dict) -> None:
    """I1/I2: interrupt a child that is genuinely mid-LLM-turn."""
    import psutil

    handle = manager.spawn_tracked(
        INTERRUPT_TASK,
        LONG_PROMPT,
        llm_no=llm_no,
        verbose=False,
        parent_session_id="real_s12_subagent_e2e",
    )
    name = handle.task_name
    task_dir = Path(handle.task_dir)
    row: dict[str, Any] = {"taskName": name, "pid": handle.pid}
    summary["interrupt"] = row

    # "Running" is written *before* `agent.put_task`, so on its own it does not mean the provider
    # request has gone out. Kept as a cheap first gate; the real one is the log below.
    started = wait_for(lambda: read_state(task_dir).get("turn_status") == "running", timeout_s=120)
    row["reachedRunning"] = bool(started)
    if not started:
        summary["issues"].append("child_never_started_a_real_turn")
        return
    # This is the gate the negative control forced. Earlier candidates and why they are wrong:
    #   - >200 streamed bytes in output.txt: timed out at 180s, the model buffers before flushing
    #   - output.txt exists at all: never appeared in 180s of a genuinely running turn
    #   - turn_status == running: reached before the request is sent, so the pre-fix build passed
    # `_write_llm_log('Prompt', ...)` fires on the line before `backend.ask(...)`, i.e. once the
    # loop is inside `exhaust(response_gen)` where no chunk will be yielded until the call returns.
    log_path = wait_for(lambda: provider_request_sent(PROMPT_PROBE_MARKER), timeout_s=180, interval_s=0.2)
    row["providerRequestLogged"] = bool(log_path)
    if not log_path:
        summary["issues"].append("child_never_sent_its_provider_request")
        return
    row["outputFileAtInterrupt"] = (task_dir / "output.txt").exists()
    row["pidAliveBeforeInterrupt"] = psutil.pid_exists(int(handle.pid))
    row["turnStatusAtInterrupt"] = read_state(task_dir).get("turn_status")
    if row["turnStatusAtInterrupt"] != "running":
        # Interrupting a turn that already ended is the trap the first run fell into: it
        # measures nothing, and would pass even with the bug reinstated.
        summary["issues"].append("turn_already_finished_before_the_interrupt_was_sent")
    out_path = task_dir / "output.txt"
    bytes_at_interrupt = out_path.stat().st_size if out_path.exists() else 0

    sent_at = time.monotonic()
    manager.interrupt_agent(name, reason="real_s12_interrupt")
    # `interrupt_agent` is not `close_agent`: the documented contract is "中断当前轮次；子进程会
    # 尽量保留以便后续 followup_task". Measured on run 4, which asserted agent_shutdown and
    # process exit and failed: the interrupt did work (events went
    # turn_started → interrupt_requested → turn_completed, 9s apart, with an empty answer), but
    # agent_shutdown is only emitted on the close path. So the end of the turn is what proves the
    # fix, and the child staying alive is part of the contract, not a failure.
    ended = wait_for(lambda: "turn_completed" in event_types(task_dir), timeout_s=60, interval_s=0.1)
    row["secondsToTurnEnd"] = None if not ended else round(time.monotonic() - sent_at, 3)
    row["turnEnded"] = bool(ended)
    if not ended:
        summary["issues"].append("interrupt_never_ended_the_running_turn")

    final_state = read_state(task_dir)
    row["turnStatusAfterInterrupt"] = final_state.get("turn_status")
    row["processStatusAfterInterrupt"] = final_state.get("process_status")
    row["pidStillAliveForFollowup"] = psutil.pid_exists(int(handle.pid))
    if not row["pidStillAliveForFollowup"]:
        # An interrupt that kills the child is close_agent's job, and it would silently break
        # the followup_task the tool description promises.
        summary["issues"].append("interrupt_killed_the_child_instead_of_preserving_it")

    text = output_text(task_dir)
    row["bytesAtInterrupt"] = bytes_at_interrupt
    row["finalOutputBytes"] = len(text.encode("utf-8"))
    # The prompt asks for 2000 lines; a turn-boundary-only interrupt would have let it finish.
    row["reachedFinalLine"] = LONG_PROMPT_FINAL_LINE in text
    if row["reachedFinalLine"]:
        summary["issues"].append("interrupt_did_not_stop_the_turn_before_it_finished")
    # An interrupt that only lands after the model is done is indistinguishable from no
    # interrupt at all, so the latency itself is asserted. Reference point: this answer needs
    # minutes, and run 4 measured 9s from interrupt to turn end.
    if row["secondsToTurnEnd"] is not None and row["secondsToTurnEnd"] > 30:
        summary["issues"].append("interrupt_latency_looks_like_turn_boundary_not_midturn")


def check_malformed_history(manager, llm_no: int, summary: dict) -> None:
    """H1: a corrupt `_history.json` must cost the forked context, not the whole run."""
    handle = manager.spawn_tracked(
        HISTORY_TASK,
        SHORT_PROMPT.format(marker=HISTORY_MARKER),
        llm_no=llm_no,
        verbose=False,
        parent_session_id="real_s12_subagent_e2e",
        corrupt_file=lambda task_dir: (task_dir / "_history.json", CORRUPT_HISTORY),
    )
    name = handle.task_name
    task_dir = Path(handle.task_dir)
    row: dict[str, Any] = {"taskName": name, "pid": handle.pid}
    summary["malformedHistory"] = row

    produced = wait_for(lambda: HISTORY_MARKER in output_text(task_dir), timeout_s=300)
    row["turnCompleted"] = bool(produced)
    if not produced:
        row["state"] = sanitize(read_state(task_dir))
        row["events"] = event_types(task_dir)
        summary["issues"].append("corrupt_history_still_killed_the_real_child")
        return

    failures = events_of(task_dir, "history_load_failed")
    row["historyLoadFailedEvents"] = len(failures)
    if len(failures) != 1:
        summary["issues"].append("corrupt_history_was_not_recorded_as_an_event")
    else:
        row["error"] = sanitize(str(failures[0].get("error", "")))[:160]
        row["quarantinePath"] = failures[0].get("quarantine_path")

    kept = sorted(p.name for p in task_dir.glob("_history.json.malformed.*"))
    row["quarantinedFiles"] = kept
    row["quarantineBytesMatch"] = bool(
        kept and (task_dir / kept[0]).read_text(encoding="utf-8", errors="replace") == CORRUPT_HISTORY
    )
    if not row["quarantineBytesMatch"]:
        summary["issues"].append("corrupt_history_bytes_were_lost")
    row["originalRemoved"] = not (task_dir / "_history.json").exists()


def check_malformed_state(manager, llm_no: int, summary: dict) -> None:
    """H2: a corrupt `state.json` is copied aside before the child's first write replaces it.

    Launches `agentmain.py` directly instead of going through `spawn_agent`, because the
    manager writes `state.json` twice *after* Popen (pid, then last_event_seq) — measured: a
    corruption injected at the Popen seam was overwritten by the parent before the child ever
    read it, so the check silently passed on healthy bytes. Driving the child directly is also
    the more faithful shape: what this fix protects against is a child inheriting a truncated
    state file, whoever truncated it.
    """
    import subprocess

    task_dir = REPO / "temp" / STATE_TASK
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "input.txt").write_text(SHORT_PROMPT.format(marker=STATE_MARKER), encoding="utf-8")
    healthy = {
        "schema_version": 1,
        "task_name": STATE_TASK,
        "agent_path": f"/root/{STATE_TASK}",
        "run_id": "run_s12_state_probe",
        "parent_session_id": "real_s12_subagent_e2e",
        "artifact_dir": str(task_dir),
        "llm_no": llm_no,
        "round": 0,
        "turn_status": "pending",
        "process_status": "alive",
    }
    raw = json.dumps(healthy, ensure_ascii=False, indent=2)
    corrupt = raw[: raw.find('"parent_session_id"')] + '"parent_sess'
    (task_dir / "state.json").write_text(corrupt, encoding="utf-8")

    row: dict[str, Any] = {"taskName": STATE_TASK, "corruptBytes": len(corrupt)}
    summary["malformedState"] = row

    cmd = [
        sys.executable,
        str(REPO / "agentmain.py"),
        "--task",
        STATE_TASK,
        "--nobg",
        "--task_root",
        str(REPO),
        "--llm_no",
        str(llm_no),
    ]
    with open(task_dir / "stdout.log", "w", encoding="utf-8") as out, open(task_dir / "stderr.log", "w", encoding="utf-8") as err:
        proc = subprocess.Popen(cmd, cwd=str(REPO), stdout=out, stderr=err)
    row["pid"] = proc.pid

    try:
        produced = wait_for(lambda: STATE_MARKER in output_text(task_dir), timeout_s=300)
        row["turnCompleted"] = bool(produced)
        if not produced:
            row["state"] = sanitize(read_state(task_dir))
            row["events"] = event_types(task_dir)
            summary["issues"].append("corrupt_state_stopped_the_real_child")
            return

        failures = events_of(task_dir, "state_load_failed")
        row["stateLoadFailedEvents"] = len(failures)
        if len(failures) != 1:
            summary["issues"].append("corrupt_state_was_not_recorded_as_an_event")
        kept = sorted(p.name for p in task_dir.glob("state.json.malformed.*"))
        row["quarantinedFiles"] = kept
        if not kept:
            summary["issues"].append("corrupt_state_was_overwritten_without_a_copy")
        else:
            preserved = (task_dir / kept[0]).read_text(encoding="utf-8", errors="replace")
            row["quarantineBytesMatch"] = preserved == corrupt
            row["quarantineKeepsRunId"] = "run_s12_state_probe" in preserved
            if not row["quarantineKeepsRunId"]:
                summary["issues"].append("quarantined_state_lost_the_run_id_evidence")
        # Measured on run 4: the marker can appear in a partial `output_snapshot` write before
        # the loop flips turn_status, so reading the field once here saw `running` and reported a
        # corrupt-state failure that did not exist. What the fix has to guarantee is that the
        # rebuilt state.json is usable *after* the turn, so wait for that.
        row["stateUsableAfterRun"] = bool(
            wait_for(lambda: read_state(task_dir).get("turn_status") == "completed", timeout_s=60)
        )
        if not row["stateUsableAfterRun"]:
            summary["issues"].append("state_json_unusable_after_the_run")
    finally:
        (task_dir / "_stop").write_text("real_s12_state_done", encoding="utf-8")
        try:
            proc.wait(timeout=30)
        except Exception:
            proc.kill()


def main() -> int:
    summary: dict[str, Any] = {
        "passed": False,
        "skipped": False,
        "expectedModel": EXPECTED_MODEL,
        "expectedProfile": EXPECTED_PROFILE,
        "issues": [],
    }
    if not OPT_IN:
        summary.update({"skipped": True, "reason": "set GA_RUN_REAL_API_E2E=1 to run the S12 E2E"})
        print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
        return 0

    manager = None
    spawned: list[str] = []
    try:
        from subagent_manager import SubagentManager

        llm_no, profile_row = resolve_profile()
        summary["profile"] = profile_row
        import psutil

        summary["psutilVersion"] = getattr(psutil, "__version__", "")
        manager = _instrumented_manager(SubagentManager(root_dir=REPO, python_executable=sys.executable), spawned)

        check_interrupt(manager, llm_no, summary)
        check_malformed_history(manager, llm_no, summary)
        check_malformed_state(manager, llm_no, summary)

        summary["passed"] = not summary["issues"]
        print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
        return 0 if summary["passed"] else 2
    except Exception as exc:
        summary["error"] = sanitize(f"{type(exc).__name__}: {exc}")
        if "exception" not in summary["issues"]:
            summary["issues"].append("exception")
        print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
        return 1
    finally:
        for name in spawned:
            if manager is not None:
                with contextlib.suppress(Exception):
                    manager.close_agent(name, reason="real_s12_e2e_cleanup", grace_s=0.5)


if __name__ == "__main__":
    raise SystemExit(main())
