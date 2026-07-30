"""Real-API E2E for the M6/M7 control-plane guards and B2 submission replay.

The unit suite proves each guard with an injected `process_exists`, which is exactly the part
that cannot be trusted on its own: the guards decide rename-vs-refuse from real process
liveness via `psutil.pid_exists`, and every unit test stubs that away. This script spawns a
real subagent process that completes a real LLM turn and then sits in `waiting_reply`, so the
guards run against a genuinely live OS process, a real registry row and a real pid.

What it verifies end to end:
  M6  spawning onto the live agent's name is refused, no second process is launched
  M6  the tool layer hands the model actionable guidance without traceback noise
  M6  once the agent is closed, the same name renames and the closed agent's artifacts survive
  M7  resuming the still-live agent is refused and its state.json pid is not overwritten
  M7  a replayed resume (same submission_id) does not start a second process
  B2  a replayed followup_task does not make the child run the task twice

Opt in with GA_RUN_REAL_API_E2E=1. Model defaults to the `cc-opus-5` profile (claude-opus-5).
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

TASK = os.environ.get("GA_GUARD_E2E_TASK") or f"guard_e2e_{int(time.time())}"
FIRST_MARKER = "GA_GUARD_E2E_FIRST_20260730"
FOLLOWUP_MARKER = "GA_GUARD_E2E_FOLLOWUP_20260730"
RESUME_MARKER = "GA_GUARD_E2E_RESUME_20260730"
RENAME_MARKER = "GA_GUARD_E2E_RENAME_20260730"

PROMPT = "你是 GA subagent 控制面守卫回归测试。不要调用工具，不要输出 Markdown。只输出这一行精确文本：{marker}"


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


def wait_for(predicate, timeout_s: float, interval_s: float = 0.5):
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


def output_contains(task_dir: Path, marker: str):
    for path in sorted(task_dir.glob("output*.txt")):
        try:
            if marker in path.read_text(encoding="utf-8", errors="replace"):
                return path.name
        except OSError:
            continue
    return None


def live_child_pids(task_name: str) -> set[int]:
    """Real agentmain.py child processes for one task, straight from the OS.

    The point of this script is that liveness is not stubbed, so the process count that decides
    "did the refused op still launch something" has to come from the OS too, not from a popen
    counter the test controls.
    """
    try:
        import psutil
    except ImportError:
        return set()
    pids = set()
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = proc.info.get("cmdline") or []
        except Exception:
            continue
        if any("agentmain.py" in str(part) for part in cmdline) and any(task_name == str(part) for part in cmdline):
            pids.add(int(proc.info["pid"]))
    return pids


def count_trigger_rows(task_dir: Path) -> int:
    path = task_dir / "mailbox.jsonl"
    if not path.exists():
        return 0
    rows = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            if json.loads(line).get("trigger_turn"):
                rows += 1
        except Exception:
            continue
    return rows


def spawn_via_tool_layer(manager, task_name: str, message: str, llm_no: int) -> dict:
    """Drive `do_spawn_agent` so the refusal is checked as the model actually receives it.

    Calling the manager directly would only prove the exception is raised; the thing that was
    fixed on top of it is `_subagent_error_result()` stripping `format_error`'s traceback
    suffix, and that only exists on the tool path.
    """
    from agent_loop import exhaust
    from ga import GenericAgentHandler

    class Parent:
        verbose = False
        permission_mode = None
        task_dir = None
        session_id = "real_guard_subagent_e2e"
        llm_no = 0
        llmclient = type("Client", (), {"backend": type("Backend", (), {"history": []})()})()

    handler = GenericAgentHandler(Parent(), last_history=[], cwd=str(REPO / "temp"))
    handler.subagent_manager = manager
    outcome = exhaust(
        handler.do_spawn_agent({"task_name": task_name, "message": message, "llm_no": llm_no, "fork_turns": "none"}, response=None)
    )
    return outcome.data


def main() -> int:
    summary: dict[str, Any] = {
        "passed": False,
        "skipped": False,
        "expectedModel": EXPECTED_MODEL,
        "expectedProfile": EXPECTED_PROFILE,
        "issues": [],
    }
    if not OPT_IN:
        summary.update({"skipped": True, "reason": "set GA_RUN_REAL_API_E2E=1 to run the control-plane guard E2E"})
        print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
        return 0

    manager = None
    spawned_names: list[str] = []
    try:
        from subagent_manager import SubagentManager
        from subagent_registry import SubagentNameConflictError

        llm_no, profile_row = resolve_profile()
        summary["profile"] = profile_row
        import psutil

        summary["psutilVersion"] = getattr(psutil, "__version__", "")

        manager = SubagentManager(root_dir=REPO, python_executable=sys.executable)
        first = manager.spawn_agent(
            TASK,
            PROMPT.format(marker=FIRST_MARKER),
            llm_no=llm_no,
            verbose=False,
            parent_session_id="real_guard_subagent_e2e",
            parent_permission_mode="read_only",
        )
        spawned_names.append(first.task_name)
        task_dir = REPO / "temp" / first.task_name
        summary["first"] = {
            "taskName": first.task_name,
            "agentPath": first.agent_path,
            "runId": first.run_id,
            "pid": first.pid,
        }
        # Every later op must target the name that was actually created. A leftover task dir
        # from an interrupted earlier run makes spawn land on `<TASK>_1`, and then a guard check
        # aimed at TASK would be testing a name nobody holds — the guard would rightly allow it
        # and the script would report a false failure.
        name = first.task_name
        summary["taskName"] = name

        # The guards read real liveness, so wait until the child has genuinely finished a real
        # LLM turn and is parked in waiting_reply — that is the state a model would retry into.
        if not wait_for(lambda: output_contains(task_dir, FIRST_MARKER), timeout_s=300):
            summary["issues"].append("first_turn_marker_missing")
            summary["firstTurnState"] = sanitize(read_state(task_dir))
            raise RuntimeError("first turn did not produce its marker")
        summary["firstOutputFile"] = output_contains(task_dir, FIRST_MARKER)
        if not wait_for(lambda: read_state(task_dir).get("process_status") == "waiting_reply", timeout_s=120):
            summary["issues"].append("child_never_reached_waiting_reply")
        summary["realPidAlive"] = psutil.pid_exists(int(first.pid))
        if not summary["realPidAlive"]:
            summary["issues"].append("child_process_not_actually_alive")

        # ---- M6: spawning onto the live name is refused, and nothing new is launched ----
        pids_before = live_child_pids(name)
        try:
            manager.spawn_agent(name, PROMPT.format(marker=RENAME_MARKER), llm_no=llm_no)
            summary["issues"].append("live_name_spawn_was_not_refused")
            summary["m6"] = {"refused": False}
        except SubagentNameConflictError as exc:
            summary["m6"] = {
                "refused": True,
                "message": sanitize(str(exc)),
                "agentPath": getattr(exc, "agent_path", None),
                "namesGuidance": all(word in str(exc) for word in ("followup_task", "close_agent")),
            }
            if not summary["m6"]["namesGuidance"]:
                summary["issues"].append("refusal_does_not_name_the_alternatives")
        time.sleep(2)
        pids_after = live_child_pids(name)
        summary["m6"]["newProcessesAfterRefusal"] = sorted(pids_after - pids_before)
        if pids_after - pids_before:
            summary["issues"].append("refused_spawn_still_launched_a_real_process")
        if not (REPO / "temp" / f"{name}_1" / "state.json").exists():
            summary["m6"]["noStrayTaskDir"] = True
        else:
            summary["m6"]["noStrayTaskDir"] = False
            summary["issues"].append("refused_spawn_left_a_renamed_task_dir")

        # ---- M6 tool layer: the model must get guidance, not a traceback ----
        tool_result = spawn_via_tool_layer(manager, name, PROMPT.format(marker=RENAME_MARKER), llm_no)
        summary["m6Tool"] = {
            "status": tool_result.get("status"),
            "reason": tool_result.get("reason"),
            "agentPath": tool_result.get("agent_path"),
            "msg": sanitize(tool_result.get("msg", "")),
        }
        if tool_result.get("reason") != "name_conflict":
            summary["issues"].append("tool_layer_did_not_tag_the_conflict")
        if " -> `" in str(tool_result.get("msg", "")) or "SubagentNameConflictError:" in str(tool_result.get("msg", "")):
            summary["issues"].append("tool_layer_leaked_traceback_noise_to_the_model")

        # ---- M7: resuming the live agent is refused and does not steal its pid ----
        pid_before_resume = read_state(task_dir).get("pid")
        pids_before = live_child_pids(name)
        try:
            manager.resume_agent(name, PROMPT.format(marker=RESUME_MARKER))
            summary["issues"].append("resume_of_live_agent_was_not_refused")
            summary["m7Live"] = {"refused": False}
        except SubagentNameConflictError as exc:
            summary["m7Live"] = {"refused": True, "message": sanitize(str(exc))}
        time.sleep(2)
        summary["m7Live"]["pidUnchanged"] = read_state(task_dir).get("pid") == pid_before_resume
        summary["m7Live"]["newProcesses"] = sorted(live_child_pids(name) - pids_before)
        if not summary["m7Live"]["pidUnchanged"]:
            summary["issues"].append("refused_resume_overwrote_the_live_pid")
        if live_child_pids(name) - pids_before:
            summary["issues"].append("refused_resume_still_launched_a_real_process")

        # ---- B2 on a real child: a replayed followup must not run the task twice ----
        triggers_before = count_trigger_rows(task_dir)
        manager.followup_task(name, PROMPT.format(marker=FOLLOWUP_MARKER), submission_id="real_e2e_followup")
        manager.followup_task(name, PROMPT.format(marker=FOLLOWUP_MARKER), submission_id="real_e2e_followup")
        summary["b2"] = {"triggerRowsAdded": count_trigger_rows(task_dir) - triggers_before}
        if summary["b2"]["triggerRowsAdded"] != 1:
            summary["issues"].append("replayed_followup_queued_the_task_twice")
        if not wait_for(lambda: output_contains(task_dir, FOLLOWUP_MARKER), timeout_s=300):
            summary["issues"].append("followup_turn_marker_missing")
        summary["b2"]["followupOutputFile"] = output_contains(task_dir, FOLLOWUP_MARKER)

        # ---- M7 replay: close first (resume needs a dead agent), then replay one submission ----
        manager.close_agent(name, reason="real_guard_e2e_before_resume", grace_s=1.0)
        first_output = (task_dir / "output.txt").read_text(encoding="utf-8", errors="replace") if (task_dir / "output.txt").exists() else ""
        pids_before = live_child_pids(name)
        resumed = manager.resume_agent(name, PROMPT.format(marker=RESUME_MARKER), submission_id="real_e2e_resume")
        replay = manager.resume_agent(name, PROMPT.format(marker=RESUME_MARKER), submission_id="real_e2e_resume")
        time.sleep(3)
        summary["m7Replay"] = {
            "firstPid": resumed.handle.pid,
            "replayPid": replay.handle.pid,
            "samePid": resumed.handle.pid == replay.handle.pid,
            "sameRunId": resumed.handle.run_id == replay.handle.run_id,
            "runIdMatchesOriginal": resumed.handle.run_id == first.run_id,
            "newProcessCount": len(live_child_pids(name) - pids_before),
        }
        if not summary["m7Replay"]["samePid"] or not summary["m7Replay"]["sameRunId"]:
            summary["issues"].append("replayed_resume_returned_a_different_process")
        if summary["m7Replay"]["newProcessCount"] > 1:
            summary["issues"].append("replayed_resume_started_a_second_real_process")
        if not wait_for(lambda: output_contains(task_dir, RESUME_MARKER), timeout_s=300):
            summary["issues"].append("resume_turn_marker_missing")
        summary["m7Replay"]["resumeOutputFile"] = output_contains(task_dir, RESUME_MARKER)

        # ---- M6 other half: once the agent is closed, the name renames and artifacts survive ----
        manager.close_agent(name, reason="real_guard_e2e_done", grace_s=1.0)
        renamed = manager.spawn_agent(name, PROMPT.format(marker=RENAME_MARKER), llm_no=llm_no, parent_session_id="real_guard_subagent_e2e")
        spawned_names.append(renamed.task_name)
        renamed_dir = Path(renamed.task_dir)
        summary["m6Rename"] = {
            "taskName": renamed.task_name,
            "agentPath": renamed.agent_path,
            "separateTaskDir": renamed_dir != task_dir,
            "originalArtifactIntact": (task_dir / "output.txt").exists()
            and (task_dir / "output.txt").read_text(encoding="utf-8", errors="replace") == first_output,
        }
        if renamed.task_name == TASK or not summary["m6Rename"]["separateTaskDir"]:
            summary["issues"].append("closed_name_was_reused_instead_of_renamed")
        if first_output and not summary["m6Rename"]["originalArtifactIntact"]:
            summary["issues"].append("closed_agent_artifacts_were_clobbered")
        if not wait_for(lambda: output_contains(renamed_dir, RENAME_MARKER), timeout_s=300):
            summary["issues"].append("renamed_agent_turn_marker_missing")
        summary["m6Rename"]["outputFile"] = output_contains(renamed_dir, RENAME_MARKER)

        # ---- spawn_rejected events must record every refusal ----
        rejections = [e for e in manager.event_bus.read_events_since(0) if e.get("type") == "spawn_rejected"]
        summary["spawnRejectedEvents"] = len(rejections)
        if summary["spawnRejectedEvents"] < 3:
            # one manager-level spawn + one tool-layer spawn + one live resume
            summary["issues"].append("not_every_refusal_was_recorded_as_an_event")

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
        for name in spawned_names:
            if manager is not None:
                with contextlib.suppress(Exception):
                    manager.close_agent(name, reason="real_guard_e2e_cleanup", grace_s=0.5)


if __name__ == "__main__":
    raise SystemExit(main())
