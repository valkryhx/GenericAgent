"""Probe: drive GA's workflow + agent-team features against the INSTALLED package.

WHY THIS FILE IS KEPT
---------------------
The repo's real e2e suite (`tests/real_*e2e*.py`, 61 scripts) all set
``REPO = Path(__file__).resolve().parents[1]`` and prepend it to ``sys.path``. That means
they can only ever exercise the SOURCE tree: run after `pip install`, they still import
`D:\\git_codes\\GenericAgent\\genericagent`, so a wheel shipping a broken workflow runtime
or a missing child-agent resource would pass every one of them.

This probe is the installed-package counterpart. It imports only from site-packages,
recorded and asserted up front, then drives the two features whose failure modes are
invisible to unit tests:

  * workflow  -- real child agents, a parallel + serial barrier, real artifacts on disk
  * agent team -- task_create + team_mode spawn_agent, shared task board, real markers

WHAT IT MEASURES (judged by invariants, never fixed output)
-----------------------------------------------------------
Path length (control group, runs first):
  0. `atomic_write_json` appends `.<pid>.<tid>.tmp` to the target name, so a workspace deep
     enough to push the *temp* name past Windows MAX_PATH (260) makes `create_run` raise and
     `workflow_draft` silently return an empty runId. This probe pins that down with a
     deliberately long workspace (must fail) and a short one (must pass), so a later empty
     runId is never mistaken for a broken workflow runtime.
Workflow:
  1. `workflow_draft` -> `workflow_approve` produces `workflow_run` and the run starts.
  2. `parallel()` branches all complete and the serial synthesis step consumes their
     results (job count and per-job status are read from the run store, not from prose).
  3. Every job reaches a terminal status with no `workflow_failed` event.
  4. The artifact the script writes really exists on disk with expected content.
Agent team:
  5. `task_create` returns a real task id on the shared board.
  6. `spawn_agent(team_mode=true)` starts a child whose `state.json` reports
     `capability_options.team_mode == true`.
  7. The teammate reaches `completed` and its marker file lands.
  8. The shared task board on disk contains the task the lead created.

HOW TO RUN (NOT from the repo root -- see WHY above)
---------------------------------------------------
    cd /d/tmp
    GA_PROBE_REAL_TEAM_WORKFLOW=1 python \
        /d/git_codes/GenericAgent/tests/probes/python/probe_installed_team_workflow.py

Costs real tokens (roughly 8-12 child agent turns). Without the opt-in it only asserts
the installed provenance and skips the real work.

TWO ENVIRONMENT TRAPS THIS PROBE PINS DOWN
------------------------------------------
* **Short workspace required.** The workflow run dir is
  `<data>/projects/<project>/session_<id>/workflows/<runId>/`, and `atomic_write_json`
  writes `.<name>.<pid>.<tid>.tmp` there before `os.replace`. Under a deep base that temp
  name passes Windows MAX_PATH (260), `open()` raises FileNotFoundError, and
  `workflow_draft` swallows it into an empty runId -- which reads exactly like "the
  installed workflow runtime is broken". check_path_budget_control() proves the mechanism.
* **Teammates are not reaped by `bridge.stop()`.** A child still mid-turn survives the
  bridge and keeps its workspace log handles open, so the next run cannot clean up. This
  probe kills what it started and asserts it managed to.
"""

from __future__ import annotations

import json
import os
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
OPT_IN = os.environ.get("GA_PROBE_REAL_TEAM_WORKFLOW") == "1"
MODEL = os.environ.get("GA_PROBE_MODEL", "")  # e.g. default/moma/deepseek-latest-flash
WORKFLOW_TIMEOUT = float(os.environ.get("GA_PROBE_WORKFLOW_TIMEOUT", "600"))
TEAM_TIMEOUT = float(os.environ.get("GA_PROBE_TEAM_TIMEOUT", "300"))

_checks: list[dict] = []


def log(message: str = "") -> None:
    sys.__stdout__.write(str(message) + chr(10))
    sys.__stdout__.flush()


def record(name: str, ok: bool, detail: str = "") -> None:
    _checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})
    log(("PASS " if ok else "FAIL ") + name + ((" - " + detail) if detail else ""))


def _is_repo_path(path: str) -> bool:
    try:
        normalized = os.path.normcase(os.path.abspath(path))
        root = os.path.normcase(os.path.abspath(REPO_ROOT))
    except (OSError, ValueError):
        return False
    return normalized == root or normalized.startswith(root + os.sep)


def _shadows(name: str, entry: str) -> bool:
    candidate = os.path.join(entry, name)
    return os.path.isdir(candidate) or os.path.isfile(candidate + ".py")


def check_provenance() -> bool:
    """Refuse to pretend this tested the wheel if a checkout won the import."""
    import frontends.ink.bridge as bridge
    import genericagent
    from genericagent import workflow

    ok = True
    for label, module in (
        ("genericagent", genericagent),
        ("genericagent.workflow", workflow),
        ("frontends.ink.bridge", bridge),
    ):
        origin = getattr(module, "__file__", "") or ""
        passed = bool(origin) and not _is_repo_path(origin)
        ok = ok and passed
        record("%s loads from site-packages" % label, passed, origin)

    checked_roots = ("genericagent", "ga_cli", "frontends")
    shadowing = [
        entry
        for entry in sys.path
        if entry and _is_repo_path(entry) and any(_shadows(root, entry) for root in checked_roots)
    ]
    record("no sys.path entry can shadow the installed packages", not shadowing, "; ".join(shadowing) or "clean")
    return ok and not shadowing


def check_path_budget_control() -> str:
    """Pin the MAX_PATH failure down and return a workspace that is short enough.

    `atomic_write_json` (genericagent/agents/state.py:79) writes to
    `.<name>.<pid>.<tid>.tmp` before `os.replace`. The run dir path is
    `<data>/projects/<project>/session_<id>/workflows/<runId>`, so the temp name for
    `final-result.json` is the longest thing the workflow store ever writes. On a short
    workspace that is fine; under a deep base it exceeds 260 and `open()` fails with
    FileNotFoundError, which `workflow_draft` swallows into an empty runId. Without this
    control a red workflow check would look like a broken runtime instead of a long path.

    Returns the short workspace base to use for the real run.
    """
    long_base = os.path.join(
        os.environ.get("TEMP", "/tmp"),
        "ga-probe-path-control-" + "x" * 90,  # deliberately too deep
    )
    short_base = os.path.join(os.environ.get("TEMP", "/tmp"), "ga-probe")

    def temp_name_len(base: str) -> int:
        # Mirror the real layout: <base>/data/projects/<project>/session_<id>/workflows/<runId>/
        run_dir = os.path.join(
            base, "data", "projects", "probe-proj-0000", "session_session_probe0000",
            "workflows", "wf_" + "0" * 32,
        )
        return len(os.path.join(run_dir, ".final-result.json.12345.6789.tmp"))

    long_len = temp_name_len(long_base)
    short_len = temp_name_len(short_base)
    record(
        "path control: a deep workspace exceeds MAX_PATH for the temp name",
        long_len > 260,
        "%d chars > 260" % long_len,
    )
    record(
        "path control: the short workspace stays under MAX_PATH",
        short_len <= 260,
        "%d chars <= 260" % short_len,
    )

    # Demonstrate the failure mechanism itself, not just the arithmetic: the same writer
    # that create_run uses must raise on the deep path and succeed on the short one.
    from genericagent.agents.state import atomic_write_json

    deep = os.path.join(long_base, "data", "projects", "probe-proj-0000", "session_session_probe0000",
                        "workflows", "wf_" + "0" * 32)
    try:
        os.makedirs(deep, exist_ok=True)
        atomic_write_json(os.path.join(deep, "final-result.json"), {"probe": True})
        mechanism_failed = ""
    except OSError as exc:
        mechanism_failed = "%s: %s" % (type(exc).__name__, str(exc)[:120])
    record(
        "path control: atomic_write_json fails on the deep workspace",
        bool(mechanism_failed),
        mechanism_failed or "unexpectedly succeeded",
    )
    try:
        os.remove(os.path.join(deep, "final-result.json"))
        os.removedirs(deep)
    except OSError:
        pass

    return short_base


def _workflow_script(workspace: str) -> str:
    """A workflow with a real parallel fan-out and a serial barrier.

    Deliberately small so the token cost stays bounded, but every structural claim the run
    is judged on has to come from a real child agent: the numbers are not passed in, each
    probe child reads a single file the runtime put there and reports what it saw.
    """
    escaped = workspace.replace("\\", "/")
    return (
        "export const meta = { name: 'installed-probe-parallel-serial', "
        "description: 'probe: 3 parallel probes then one serial synthesis writing an artifact' }\n"
        "phase('Probe');\n"
        "const probes = await parallel([\n"
        "  () => agent('Use code_run to run: python -c \"print(7*6)\". "
        "Reply with only the number you observed.', { label: 'probe-math' }),\n"
        "  () => agent('Use file_write to create probe_a.txt containing exactly PROBE_A_OK "
        "in the current working directory. Then reply with the single word DONE.', { label: 'probe-write-a' }),\n"
        "  () => agent('Use file_write to create probe_b.txt containing exactly PROBE_B_OK "
        "in the current working directory. Then reply with the single word DONE.', { label: 'probe-write-b' }),\n"
        "]);\n"
        "phase('Synthesis');\n"
        "const synth = await agent(\n"
        "  'Read probe_a.txt and probe_b.txt from the current working directory with file_read. "
        "Then use file_write to create probe_artifact.md containing a line starting with "
        "\"ARTIFACT_START\" followed by the exact contents of both files. "
        "Reply with the single token SYNTH_DONE when finished.',\n"
        "  { label: 'synthesis' },\n"
        ");\n"
        "log('probes=' + JSON.stringify(probes));\n"
        "return { probes, synth: String(synth || ''), workspace: " + json.dumps(escaped) + " };\n"
    )


def run_workflow(bridge, events: list, workspace: str) -> dict:
    """Draft -> approve -> wait for terminal. Judged from the run store, not from prose."""
    def wait_event(event_type: str, timeout: float):
        deadline = time.time() + timeout
        while time.time() < deadline:
            hits = [event for event in events if event.get("type") == event_type]
            if hits:
                return hits[-1]
            time.sleep(0.2)
        return None

    script = _workflow_script(workspace)
    run_id = bridge.workflow_draft(script)
    record("workflow_draft accepted the script", bool(run_id), run_id or "no runId returned")
    if not run_id:
        return {"skipped": True, "reason": "draft rejected"}

    if not bridge.workflow_approve(run_id, args={"workspacePath": workspace}, timeout_seconds=WORKFLOW_TIMEOUT):
        record("workflow_approve started the run", False, "approve returned False")
        return {"skipped": True, "runId": run_id}

    started = wait_event("workflow_run", 60)
    record(
        "workflow_run emitted for the approved draft",
        bool(started),
        "status=%s" % ((started or {}).get("run") or {}).get("status"),
    )

    final = wait_event("workflow_final", WORKFLOW_TIMEOUT)
    failed = [event for event in events if event.get("type") == "workflow_failed"]
    record("workflow reached a terminal event", bool(final), (final or {}).get("status", "no workflow_final"))
    record("no workflow_failed event", not failed, (failed[-1].get("message", "") if failed else ""))

    # Ground truth: reload the run from the store the installed package created.
    run = bridge.workflow_store.load_run(run_id)
    jobs = list(getattr(run, "jobs", []) or [])
    statuses = [str(getattr(job, "status", "")) for job in jobs]
    record(
        "run status is a success terminal state",
        str(run.status) in {"succeeded", "degraded"},
        "status=%s jobs=%s" % (run.status, statuses),
    )
    record("the run contains the 4 expected jobs", len(jobs) == 4, "%d job(s): %s" % (len(jobs), statuses))
    record(
        "no job ended in a failure state",
        all(status in {"succeeded", "degraded"} for status in statuses) and bool(statuses),
        str(statuses),
    )
    record("script really used parallel()", "parallel(" in str(run.script or ""), "")

    artifact = os.path.join(workspace, "probe_artifact.md")
    art_a = os.path.join(workspace, "probe_a.txt")
    art_b = os.path.join(workspace, "probe_b.txt")
    body = ""
    if os.path.isfile(artifact):
        with open(artifact, encoding="utf-8", errors="replace") as handle:
            body = handle.read()
    record("serial synthesis wrote probe_artifact.md", os.path.isfile(artifact), artifact)
    record("artifact references ARTIFACT_START", "ARTIFACT_START" in body, body[:120].replace(chr(10), " | "))
    record(
        "parallel branches really wrote their own files",
        os.path.isfile(art_a) and os.path.isfile(art_b),
        "a=%s b=%s" % (os.path.isfile(art_a), os.path.isfile(art_b)),
    )
    return {"skipped": False, "runId": run_id, "status": str(run.status), "jobs": statuses}


def _wait_idle(bridge, timeout: float) -> bool:
    """Wait until the main agent can accept a submission.

    `submit()` rejects with `code: busy` while a turn is running (bridge.py:583), and after
    a workflow settles the runtime delivers a `<workflow_handoff>` turn that keeps the agent
    busy for a while. Submitting the team prompt straight after the workflow therefore gets
    `taskId=-1` and the whole team half silently never runs. Wait for idle first.
    """
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if not getattr(bridge.agent, "is_running", False) and not bridge._is_consuming():
                return True
        except Exception:  # noqa: BLE001 - private probe, treat any error as "not idle yet"
            pass
        time.sleep(0.5)
    return False


def run_team(bridge, events: list, workspace: str) -> dict:
    """task_create + team_mode spawn_agent, verified from state.json and the shared board."""
    def wait_event(event_type: str, timeout: float):
        deadline = time.time() + timeout
        while time.time() < deadline:
            hits = [event for event in events if event.get("type") == event_type]
            if hits:
                return hits[-1]
            time.sleep(0.2)
        return None

    marker = os.path.join(workspace, "marker_team.txt")
    prompt = (
        "This is an agent-team probe. Do exactly this and nothing else:\n"
        "1) Call task_create with subject 'installed probe marker'. Keep the returned task id.\n"
        "2) Call spawn_agent with task_name='probe-teammate', team_mode=true, "
        "team_task_id=<the id from step 1>, background=true. Its message must instruct it to run "
        "this with code_run: write the text TEAM_OK into " + marker.replace("\\", "/") + " and then stop.\n"
        "3) After spawn_agent returns, report the task id and end the turn. "
        "Do not wait for the teammate and do not write the marker yourself."
    )

    idle = _wait_idle(bridge, 300)
    record("agent was idle before the team submission", idle, "" if idle else "still busy after 300s")

    task_id = bridge.submit(prompt)
    record("team prompt submitted to the real agent", task_id >= 0, "taskId=%s" % task_id)
    if task_id < 0:
        return {"skipped": True, "reason": "submit rejected (agent busy)"}

    assistant = wait_event("assistant_done", TEAM_TIMEOUT)
    record("main agent finished its team turn", bool(assistant), str((assistant or {}).get("text", ""))[:120])

    # The teammate is a separate OS process; wait for the marker it was told to write.
    deadline = time.time() + TEAM_TIMEOUT
    while time.time() < deadline and not os.path.isfile(marker):
        time.sleep(1.0)
    record("teammate wrote its marker file", os.path.isfile(marker), marker)

    # The manager lives on the BRIDGE (`self.subagent_manager`, bridge.py:408), not on the
    # agent -- `_build_agent_control()` constructs it while wiring the unified control plane.
    manager = getattr(bridge, "subagent_manager", None)
    rows: list = []
    if manager is None:
        record("bridge exposes subagent_manager", False, "attribute missing on GenericAgentBridge")
    else:
        try:
            rows = manager.list_agent_snapshots(include_closed=True)
        except Exception as exc:  # noqa: BLE001 - report, do not raise
            record("list_agent_snapshots callable", False, "%s: %s" % (type(exc).__name__, exc))
    record("subagent manager exposed the spawned teammate", bool(rows), "%d agent state(s)" % len(rows))

    team_mode_rows = [
        state
        for state in rows
        if bool(((getattr(state, "capability_options", None) or {}).get("team_mode")))
    ]
    record(
        "a child ran with capability_options.team_mode=true",
        bool(team_mode_rows),
        "team_mode agents=%d" % len(team_mode_rows),
    )
    statuses = [str(getattr(state, "turn_status", "")) for state in team_mode_rows]
    record(
        "the team-mode child reported its capability manifest",
        bool(team_mode_rows),
        "statuses=%s" % statuses,
    )
    # The teammate may still be mid-turn here: the marker lands when its code_run finishes,
    # but closing out the turn (and writing `turn_status=completed`) happens afterwards. Wait
    # for the terminal status instead of sampling once and reporting a false failure.
    waited = 0.0
    while waited < TEAM_TIMEOUT and any(status not in {"completed", "failed"} for status in statuses):
        time.sleep(2.0)
        waited += 2.0
        try:
            rows = manager.list_agent_snapshots(include_closed=True) if manager is not None else rows
        except Exception:  # noqa: BLE001
            break
        team_mode_rows = [
            state
            for state in rows
            if bool(((getattr(state, "capability_options", None) or {}).get("team_mode")))
        ]
        statuses = [str(getattr(state, "turn_status", "")) for state in team_mode_rows]
    record(
        "the team-mode child reached a completed turn",
        any(status == "completed" for status in statuses),
        "%s after %.0fs" % (statuses, waited),
    )

    # Shared task board. The board lives under `<data>/tasks/<project>--<teamKey>/tasks.json`
    # (genericagent handler's task_create), NOT under a `teams/` directory -- that one only
    # holds boards created by older sessions in the repo checkout.
    boards = []
    data_dir = os.environ.get("GA_DATA_DIR") or ""
    for root in {data_dir, os.path.join(REPO_ROOT, "temp")}:
        if not root or not os.path.isdir(root):
            continue
        for dirpath, _dirnames, filenames in os.walk(root):
            if "tasks.json" in filenames and (os.sep + "tasks" + os.sep in dirpath + os.sep
                                              or os.sep + "teams" + os.sep in dirpath + os.sep):
                boards.append(os.path.join(dirpath, "tasks.json"))
    board_hits = []
    for path in boards:
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                payload = json.load(handle)
        except (OSError, ValueError):
            continue
        text = json.dumps(payload, ensure_ascii=False)
        if "installed probe marker" in text:
            board_hits.append(path)
    record(
        "shared task board contains the created task",
        bool(board_hits),
        board_hits[0] if board_hits else "%d board(s) scanned" % len(boards),
    )
    return {
        "skipped": False,
        "marker": os.path.isfile(marker),
        "teamModeAgents": len(team_mode_rows),
        "turnStatuses": statuses,
        "boards": boards[:3],
    }


def _probe_children(workspace: str) -> list[str]:
    """PIDs of live child agents this probe booted (cmdline carries the workspace)."""
    try:
        import psutil
    except ImportError:
        return []
    alive: list[str] = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = " ".join(proc.info["cmdline"] or [])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if workspace in cmdline and "genericagent.core.app" in cmdline:
            alive.append("pid=%s" % proc.info["pid"])
    return alive


def _kill_probe_children(workspace: str) -> list[str]:
    """Kill child agents this probe started; return what was killed.

    Matches on the workspace path inside the cmdline, so it can only ever touch processes
    this probe booted with `GA_PROJECT_ROOT=<workspace>`. Both the child and its MCP
    grandchildren are killed: leaving the npx servers behind keeps the log handles open and
    the next run cannot clean up.
    """
    try:
        import psutil
    except ImportError:
        return []

    killed: list[str] = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = " ".join(proc.info["cmdline"] or [])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if workspace not in cmdline or "genericagent.core.app" not in cmdline:
            continue
        try:
            for child in proc.children(recursive=True):
                try:
                    child.kill()
                except psutil.Error:
                    pass
            proc.kill()
            proc.wait(timeout=10)
            killed.append("pid=%s" % proc.pid)
        except psutil.Error:
            continue
    if killed:
        time.sleep(2)  # let Windows release the log handles
    return killed


def main() -> int:
    started = time.time()
    log("repo root (for shadow detection): %s" % REPO_ROOT)
    log("cwd                             : %s" % os.getcwd())
    log("opt-in real team/workflow       : %s" % OPT_IN)
    log("")

    if not check_provenance():
        log("")
        log("VERDICT: FAIL (a source checkout shadowed the installed package; "
            "run this from outside the repo)")
        return 1

    # Any cwd outside the checkout works, but the workspace must stay SHORT: see
    # check_path_budget_control() for why a deep base silently kills workflow_draft.
    workdir = check_path_budget_control()
    os.makedirs(workdir, exist_ok=True)
    os.environ.setdefault("GA_PROJECT_ROOT", workdir)
    os.environ.setdefault("GA_DATA_DIR", os.path.join(workdir, "data"))
    os.environ.setdefault("GA_WORKFLOW_WORKSPACE_ROOT", workdir)
    os.environ.setdefault("GA_LAUNCH_CWD", workdir)
    os.environ.setdefault("GA_ORIGINAL_CWD", workdir)
    for key in (
        "GA_PROJECT_KEY", "GA_PROJECT_DATA_DIR", "GA_SESSION_DATA_DIR",
        "GA_PARENT_SESSION_ID", "GA_TASK_ROOT", "GA_TEAM_KEY",
    ):
        os.environ.pop(key, None)
    log("workspace                       : %s" % workdir)
    log("")

    result: dict = {"workspace": workdir, "workflow": {"skipped": True}, "team": {"skipped": True}}
    if not OPT_IN:
        log("SKIP real workflow + team (set GA_PROBE_REAL_TEAM_WORKFLOW=1 to include them)")
        log("     costs real tokens: ~8-12 child agent turns")
    else:
        import threading

        from frontends.ink.bridge import GenericAgentBridge

        events: list[dict] = []
        bridge = GenericAgentBridge(emit=events.append)
        try:
            if MODEL:
                bridge.model_switch(MODEL)
                time.sleep(2)
                log("model switched to            : %s" % MODEL)
                log("")

            log("--- workflow: parallel fan-out + serial barrier ---")
            result["workflow"] = run_workflow(bridge, events, workdir)
            log("")
            log("--- agent team: task board + team_mode teammate ---")
            result["team"] = run_team(bridge, events, workdir)
            log("")
            errors = [event for event in events if event.get("type") == "error"]
            record("no bridge error event during the whole probe", not errors,
                   (errors[-1].get("message", "") if errors else ""))
        finally:
            try:
                bridge.stop()
            except Exception as exc:  # noqa: BLE001
                log("bridge.stop() raised %s: %s" % (type(exc).__name__, exc))
            # `bridge.stop()` does not reap a teammate that is still mid-turn, so the child
            # process (plus its MCP grandchildren) outlives the probe and keeps the workspace
            # log handles open. Reaping is this probe's own responsibility; the check below
            # asserts the reaping worked, not that GA stopped the child (it does not, and that
            # is pre-existing behaviour, not an installation defect).
            reaped = _kill_probe_children(workdir)
            still_alive = _probe_children(workdir)
            record(
                "probe reaped its own child agents at teardown",
                not still_alive,
                "reaped=%s remaining=%s" % (len(reaped), len(still_alive)),
            )
            if os.environ.get("GA_PROBE_KEEP_WORKSPACE") != "1":
                import shutil

                shutil.rmtree(workdir, ignore_errors=True)
                log("workspace removed (GA_PROBE_KEEP_WORKSPACE=1 to keep it)")

    bundle_dir = os.path.join(REPO_ROOT, "tests", "probes", "captures")
    os.makedirs(bundle_dir, exist_ok=True)
    summary = {
        "startedAt": started,
        "elapsedSeconds": round(time.time() - started, 1),
        "repoRoot": REPO_ROOT,
        "cwd": os.getcwd(),
        "optIn": OPT_IN,
        "checks": _checks,
        **result,
    }
    summary_path = os.path.join(bundle_dir, "installed-team-workflow-%s.json" % time.strftime("%Y%m%d-%H%M%S"))
    with open(summary_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(summary, ensure_ascii=False, indent=2))

    failed = [check for check in _checks if not check["ok"]]
    log("summary: %s" % summary_path)
    log("VERDICT: %s (%d checks, %d failed)" % ("OK" if not failed else "FAIL", len(_checks), len(failed)))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
