"""Real-LLM end-to-end proof for project-scoped directories.

This driver exercises the actual GenericAgentBridge, agent process, workflow
runtime, subagent process, task board, resume path and legacy read-only views.
It uses isolated temporary projects and a temporary GA data root, so it never
moves or deletes a user's existing runtime state.

Opt in with::

    GA_RUN_REAL_PROJECT_SCOPE_E2E=1

The default model selector is the user-confirmed self-test profile:
``default/deepseek-v4.1-flash``. Evidence is written under
``temp/project-scope-e2e/<timestamp>/``.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import traceback
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTENDS = REPO_ROOT / "frontends"
for _p in (REPO_ROOT, FRONTENDS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from frontends.ink.bridge import GenericAgentBridge
from genericagent import paths
from genericagent.core.redaction import sanitize

OPT_IN = os.environ.get("GA_RUN_REAL_PROJECT_SCOPE_E2E") == "1"
MODEL_SELECTOR = os.environ.get("GA_SELFTEST_MODEL", "default/deepseek-v4.1-flash").strip()
TIMEOUT = float(os.environ.get("GA_SELFTEST_TIMEOUT", "240"))
BUNDLE_ROOT = REPO_ROOT / "temp" / "project-scope-e2e"
BACKEND_LOG = REPO_ROOT / "temp" / "ink_bridge_backend.log"


def log(message: str) -> None:
    stream = sys.__stdout__
    stream.write(str(message) + "\n")
    stream.flush()


class Recorder:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def __call__(self, event: dict[str, Any]) -> None:
        self.events.append(event)

    def of(self, event_type: str) -> list[dict[str, Any]]:
        return [event for event in self.events if event.get("type") == event_type]

    def last(self, event_type: str) -> dict[str, Any] | None:
        rows = self.of(event_type)
        return rows[-1] if rows else None


def record(checks: list[dict[str, Any]], name: str, ok: bool, detail: Any = "") -> None:
    checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})
    log(("PASS " if ok else "FAIL ") + name + ((" - " + str(detail)) if detail else ""))


def wait_for(
    rec: Recorder,
    event_type: str,
    timeout: float,
    *,
    after: int = 0,
) -> dict[str, Any] | None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        rows = rec.of(event_type)
        if len(rows) > after:
            return rows[after]
        time.sleep(0.1)
    return None


def wait_until(predicate, timeout: float, interval: float = 0.2) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def wait_bridge_idle(bridge: GenericAgentBridge, timeout: float = TIMEOUT) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if not getattr(bridge.agent, "is_running", False) and not bridge._is_consuming():
            return True
        time.sleep(0.1)
    return False


def under(path: str | Path, root: str | Path) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve())
        return True
    except (OSError, ValueError):
        return False


def set_project_environment(project: Path, data_root: Path) -> None:
    os.environ.update({
        "GA_PROJECT_ROOT": str(project),
        "GA_DATA_DIR": str(data_root),
        "GA_LAUNCH_CWD": str(project),
        "GA_ORIGINAL_CWD": str(project),
        "GA_LLM_CONFIG": str(REPO_ROOT / "llm.yaml"),
    })
    for key in (
        "GA_PROJECT_KEY",
        "GA_PROJECT_DATA_DIR",
        "GA_SESSION_DATA_DIR",
        "GA_PARENT_SESSION_ID",
        "GA_TASK_ROOT",
        "GA_TEAM_KEY",
    ):
        os.environ.pop(key, None)
def write_bundle(bundle: Path, rec: Recorder, summary: dict[str, Any]) -> None:
    bundle.mkdir(parents=True, exist_ok=True)
    with (bundle / "events.jsonl").open("w", encoding="utf-8", newline="\n") as fh:
        for event in rec.events:
            fh.write(json.dumps(sanitize(event), ensure_ascii=False) + "\n")
    try:
        lines = BACKEND_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = "\n".join(lines[-160:])
    except OSError:
        tail = ""
    (bundle / "backend-log-tail.txt").write_text(tail, encoding="utf-8", newline="\n")
    (bundle / "summary.json").write_text(
        json.dumps(sanitize(summary), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
        newline="\n",
    )


def shutdown_bridge(bridge: GenericAgentBridge) -> None:
    try:
        agent = getattr(bridge, "agent", None)
        if agent is not None:
            setattr(agent, "stop_sig", True)
            try:
                agent.abort()
            except Exception:
                pass
        bridge.stop()
        thread = getattr(bridge, "_agent_thread", None)
        if thread is not None:
            thread.join(timeout=5)
    except Exception:
        pass


def run_project_scope_child(project_a: Path, project_b: Path, data_root: Path, session_a_path: str, run_id: str, team_key: str, task_id: str) -> dict[str, Any]:
    payload = {
        "projectA": str(project_a.resolve()),
        "projectB": str(project_b.resolve()),
        "dataRoot": str(data_root.resolve()),
        "sessionAPath": session_a_path,
        "runId": run_id,
        "teamKey": team_key,
        "taskId": task_id,
    }
    env = os.environ.copy()
    result_path = REPO_ROOT / "temp" / "project-scope-e2e" / ("child-" + str(os.getpid()) + ".json")
    result_path.parent.mkdir(parents=True, exist_ok=True)
    env["GA_PROJECT_SCOPE_CHILD"] = json.dumps(payload, ensure_ascii=False)
    env["GA_PROJECT_SCOPE_RESULT_PATH"] = str(result_path)
    env["PYTHONPATH"] = str(REPO_ROOT)
    env["PYTHONIOENCODING"] = "utf-8"
    proc = __import__("subprocess").run(
        [sys.executable, str(Path(__file__).with_name("real_project_scope_child.py"))],
        cwd=str(REPO_ROOT), env=env, text=True, encoding="utf-8", errors="replace",
        capture_output=True, timeout=TIMEOUT + 60,
    )
    if not result_path.is_file():
        return {"error": f"child exit={proc.returncode}; stdout={proc.stdout[-2000:]}; stderr={proc.stderr[-2000:]}"}
    report = json.loads(result_path.read_text(encoding="utf-8"))
    report["childExitCode"] = proc.returncode
    return report


def run() -> int:
    started = time.time()
    rec = Recorder()
    checks: list[dict[str, Any]] = []
    bundle = BUNDLE_ROOT / time.strftime("%Y%m%d-%H%M%S")
    summary: dict[str, Any] = {
        "startedAt": started,
        "optIn": OPT_IN,
        "modelSelector": MODEL_SELECTOR,
        "checks": checks,
        "bundle": str(bundle),
    }
    bridge = None
    temp_root = None
    try:
        if not OPT_IN:
            record(checks, "real project-scope E2E opted in", False, "set GA_RUN_REAL_PROJECT_SCOPE_E2E=1")
            return 1

        temp_root = Path(tempfile.mkdtemp(prefix="ga-project-scope-e2e-", dir=str(REPO_ROOT / "temp")))
        project_a = temp_root / "ProjectA"
        project_b = temp_root / "ProjectB"
        data_root = temp_root / "data"
        project_a.mkdir(parents=True)
        project_b.mkdir(parents=True)
        data_root.mkdir(parents=True)
        summary["tempRoot"] = str(temp_root)

        set_project_environment(project_a, data_root)
        bridge = GenericAgentBridge(emit=rec)
        agent_a = bridge.agent
        session_a = str(agent_a.session_id or "")
        project_a_data = Path(agent_a.project_data_dir)
        summary["projectA"] = {
            "root": str(project_a),
            "key": str(agent_a.project_key),
            "dataDir": str(project_a_data),
            "sessionId": session_a,
            "sessionPath": str(agent_a.session_path),
        }
        record(checks, "A agent identity is project-scoped", agent_a.project_root == str(project_a.resolve()) and under(project_a_data, data_root))
        record(checks, "A bridge paths rebind to A", bridge.project_dir == project_a.resolve() and bridge.state_root == data_root.resolve())
        record(checks, "A transcript lives in A project bucket", under(agent_a.session_path or "", project_a_data) and Path(agent_a.session_path).is_file())
        record(checks, "A workflow store uses A bucket", Path(bridge.workflow_store.root).resolve() == project_a_data.resolve())
        record(checks, "A subagent session dir uses A bucket", under(bridge.subagent_manager.session_data_dir, project_a_data))

        model_cursor = len(rec.of("model_status"))
        bridge.model_switch(MODEL_SELECTOR)
        switch = wait_for(rec, "model_status", 30, after=model_cursor)
        current = next((item.get("name") for item in (switch or {}).get("models", []) if item.get("current")), None)
        record(checks, "default/deepseek-v4.1-flash selected", current == MODEL_SELECTOR, current)

        prompt = (
            "You are validating project-scoped filesystem behavior. "
            "Use the file_write tool exactly once to create notes.md in the current tool cwd "
            "with the exact UTF-8 content PROJECT_A_SCOPE_OK. "
            "Do not create any other file and do not use code_run. Reply with exactly done."
        )
        notes = project_a / "notes.md"
        main_attempts: list[dict[str, Any]] = []
        done = None
        usage = None
        for attempt in range(1, 4):
            assistant_cursor = len(rec.of("assistant_done"))
            usage_cursor = len(rec.of("token_usage"))
            task_id = bridge.submit(prompt)
            if task_id < 0:
                main_attempts.append({"attempt": attempt, "submitTaskId": task_id, "error": (rec.last("error") or {}).get("message", "")})
                break
            bridge.wait_for_idle(timeout=TIMEOUT)
            candidate = wait_for(rec, "assistant_done", 10, after=assistant_cursor)
            attempt_usage = wait_for(rec, "token_usage", 2, after=usage_cursor)
            candidate_text = str((candidate or {}).get("text") or "")
            transport_error = "upstream stream closed before a terminal event" in candidate_text
            main_attempts.append({
                "attempt": attempt,
                "submitTaskId": task_id,
                "transportError": transport_error,
                "assistantText": candidate_text,
                "notesPresent": notes.is_file(),
                "agentRunning": bool(getattr(bridge.agent, "is_running", False)),
            })
            if candidate is not None and not transport_error and not getattr(bridge.agent, "is_running", False):
                done = candidate
                usage = attempt_usage
                break
        summary["mainTurn"] = {
            "attempts": main_attempts,
            "assistantText": (done or {}).get("text"),
            "tokenUsage": usage,
            "notesPath": str(notes),
        }
        record(checks, "real main LLM turn completed without provider error", done is not None and bool(str((done or {}).get("text") or "").strip()))
        record(checks, "real main write landed under project A", notes.is_file() and notes.read_text(encoding="utf-8").strip() == "PROJECT_A_SCOPE_OK")
        record(checks, "A main write did not leak into B project root", not (project_b / "notes.md").exists())
        main_errors = [event for event in rec.of("error") if event.get("code") in {"busy", "put_task_failed", "consume_failed"}]
        record(checks, "no bridge lifecycle error after main turn", not main_errors, main_errors[-1].get("message", "") if main_errors else "")

        run_id = ""
        named_run_id = ""
        run = None
        named_run = None
        final_event = None
        try:
            workflow_script = """export const meta = { name: 'project-scope-e2e', description: 'Project bucket proof' }
return await agent('Reply with exactly the single token WORKFLOW_SCOPE_OK. Do not use tools.', { label: 'scope-child' })
"""
            run_id = bridge.workflow_draft(workflow_script)
            approved = bridge.workflow_approve(run_id, timeout_seconds=TIMEOUT) if run_id else False
            run = bridge.workflow_store.load_run(run_id) if run_id else None
            if run_id:
                final_cursor = len(rec.of("workflow_final"))
                def _workflow_done() -> bool:
                    loaded = bridge.workflow_store.load_run(run_id)
                    return loaded.status in {"succeeded", "degraded", "failed", "killed", "interrupted"}
                wait_until(_workflow_done, TIMEOUT)
                bridge.wait_for_workflow_idle(run_id, timeout=TIMEOUT)
                handoff_idle = wait_bridge_idle(bridge)
                run = bridge.workflow_store.load_run(run_id)
                final_event = wait_for(rec, "workflow_final", 10, after=final_cursor)
                record(checks, "workflow handoff main-agent turn settled", handoff_idle)
            summary["workflowRun"] = {
                "runId": run_id,
                "approved": approved,
                "status": getattr(run, "status", None),
                "artifactDir": str(getattr(run, "artifact_dir", "") or ""),
                "resultRef": getattr(run, "result_ref", None),
            }
            artifact_dir = Path(getattr(run, "artifact_dir", "") or "")
            record(checks, "workflow draft/approve returned a run", bool(run_id) and bool(approved), run_id)
            record(checks, "workflow run is stored under A project data", run is not None and under(artifact_dir, project_a_data))
            record(checks, "workflow artifacts include script/state/journal", all((artifact_dir / name).is_file() for name in ("script.js", "state.json", "journal.jsonl")))
            record(checks, "real workflow child completed successfully", getattr(run, "status", None) in {"succeeded", "degraded"}, getattr(run, "status", None))
            record(checks, "workflow final payload carries result", bool(getattr(run, "result_ref", None)) or final_event is not None)
            record(checks, "workflow final payload is non-empty", bool(final_event is not None or (artifact_dir / "final-result.json").is_file()))

            saved = False
            if run_id:
                saved = bridge.workflow_save(run_id, name="project-scope-named", overwrite=True)
            saved_record = bridge.workflow_store.load_named_workflow("project-scope-named") if saved else {}
            named_path = Path(str(saved_record.get("sourcePath") or saved_record.get("path") or ""))
            record(checks, "named workflow saved in A project scope", bool(saved_record) and under(named_path, project_a / ".ga" / "workflows"), named_path)
            if saved:
                named_run_id = bridge.workflow_run_named("project-scope-named", timeout_seconds=TIMEOUT)
            if named_run_id:
                wait_until(lambda: bridge.workflow_store.load_run(named_run_id).status in {"succeeded", "degraded", "failed", "killed", "interrupted"}, TIMEOUT)
                bridge.wait_for_workflow_idle(named_run_id, timeout=TIMEOUT)
                record(checks, "named workflow handoff main-agent turn settled", wait_bridge_idle(bridge))
            named_run = bridge.workflow_store.load_run(named_run_id) if named_run_id else None
            summary["namedWorkflowRun"] = {
                "runId": named_run_id,
                "status": getattr(named_run, "status", None),
                "artifactDir": str(getattr(named_run, "artifact_dir", "") or ""),
            }
            record(checks, "named workflow run uses A project data", named_run is not None and under(getattr(named_run, "artifact_dir", ""), project_a_data))
            record(checks, "named workflow completed with real child", getattr(named_run, "status", None) in {"succeeded", "degraded"}, getattr(named_run, "status", None))
        except Exception as exc:
            summary["workflowStageError"] = f"{type(exc).__name__}: {exc}"
            record(checks, "workflow stage raised no exception", False, summary["workflowStageError"])
        from genericagent.agents.team import team_name_for
        from genericagent.core.handler import GenericAgentHandler
        from genericagent.core.loop import exhaust

        handler = GenericAgentHandler(agent_a, agent_a.history, agent_a.tool_cwd, workspace_root=agent_a.project_root)
        handler.subagent_manager = bridge.subagent_manager
        task_outcome = exhaust(handler.dispatch(
            "task_create",
            {"subject": "Project-scoped team child", "description": "E2E project isolation proof"},
            None,
        ))
        team_task = (getattr(task_outcome, "data", {}) or {}).get("task") or {}
        task_id = str(team_task.get("id") or "")
        team_name = team_name_for(session_a)
        expected_team_key = paths.team_key(str(agent_a.project_key), team_name)
        task_board = data_root / "tasks" / expected_team_key / "tasks.json"
        record(checks, "task_create writes project-qualified team board", task_board.is_file() and task_id != "")

        spawn_outcome = exhaust(handler.dispatch(
            "spawn_agent",
            {
                "task_name": "team_child",
                "message": (
                    f"Your assigned team task is #{task_id}. Call task_update with taskId={task_id} "
                    "and status=completed, then reply exactly TEAM_OK. Do not use any other tools."
                ),
                "background": True,
                "fork_turns": "none",
                "permission_profile": "inherit-current-permissions",
                "team_mode": True,
                "team_task_id": task_id,
            },
            None,
        ))
        spawn_data = getattr(spawn_outcome, "data", {}) or {}
        child_state = None
        if spawn_data.get("status") == "started":
            ok = wait_until(
                lambda: bool(bridge.subagent_manager.list_agents(include_closed=True))
                and bridge.subagent_manager.list_agents(include_closed=True)[-1].turn_status in {"completed", "errored", "aborted"},
                TIMEOUT,
            )
            states = bridge.subagent_manager.list_agents(include_closed=True)
            child_state = states[-1] if states else None
        else:
            ok = False
        summary["subagent"] = {
            "spawn": spawn_data,
            "state": child_state.to_dict() if child_state is not None and hasattr(child_state, "to_dict") else None,
        }
        record(checks, "real subagent process started", spawn_data.get("status") == "started", spawn_data.get("msg", ""))
        record(checks, "real subagent completed", child_state is not None and child_state.turn_status == "completed")
        record(checks, "subagent task/state/artifact live in A bucket", child_state is not None and under(child_state.task_dir, project_a_data) and under(child_state.artifact_dir, project_a_data))
        record(checks, "subagent inherited A project session identity", child_state is not None and child_state.parent_session_id == session_a)
        record(checks, "subagent used deepseek-v4.1-flash", child_state is not None and getattr(child_state, "model_id", None) == "deepseek-v4.1-flash" and getattr(child_state, "model_profile", None) == "default")

        roster_cursor = len(rec.of("team_roster"))
        bridge.team_list()
        roster = wait_for(rec, "team_roster", 10, after=roster_cursor) or {}
        roster_tasks = roster.get("tasks") or []
        roster_members = roster.get("members") or []
        summary["teamRoster"] = roster
        record(checks, "team roster exposes project-qualified task board", any(str(t.get("id")) == task_id for t in roster_tasks))
        record(checks, "team roster exposes real teammate", any(m.get("name") == "team_child" for m in roster_members))
        record(checks, "teammate completed assigned task", any(str(t.get("id")) == task_id and t.get("status") == "completed" for t in roster_tasks))
        record(checks, "team key is project-scoped", expected_team_key.startswith(str(agent_a.project_key) + "--") and task_board.is_file())
        record(checks, "A main-agent write remains isolated after workflow/team", notes.is_file() and notes.read_text(encoding="utf-8").strip() == "PROJECT_A_SCOPE_OK")
        shutdown_bridge(bridge)
        bridge = None
        child_report = run_project_scope_child(project_a, project_b, data_root, str(agent_a.session_path), run_id, expected_team_key, task_id)
        summary["crossProjectChild"] = child_report
        record(checks, "B process uses a distinct project bucket", child_report.get("bDiffersFromA") is True, child_report.get("error", ""))
        record(checks, "B /workflows excludes A project runs", child_report.get("bWorkflowExcluded") is True)
        record(checks, "B /resume excludes A session", child_report.get("bResumeExcluded") is True)
        record(checks, "B --all does not expose A project session", child_report.get("bAllResumeExcluded") is True)
        record(checks, "B resumes A with project context rebound", child_report.get("resumeRootRebound") is True)
        record(checks, "resumed agent points at A transcript", child_report.get("resumeTranscriptRebound") is True)
        record(checks, "resumed agent restores A identity", child_report.get("resumeIdentityRestored") is True)
        record(checks, "resume rebinds workflow store to A bucket", child_report.get("resumeWorkflowRebound") is True)
        record(checks, "resume rebinds subagent manager to A bucket", child_report.get("resumeSubagentRebound") is True)
        record(checks, "resumed A session sees A workflows", child_report.get("resumeSeesWorkflow") is True)
        record(checks, "legacy session is visible only in --all", child_report.get("legacySessionVisibleAll") is True)
        record(checks, "legacy workflow is visible in --all", child_report.get("legacyWorkflowVisibleAll") is True)
        record(checks, "default workflow view still excludes legacy", child_report.get("legacyExcludedDefault") is True)
        record(checks, "legacy workflow detail opens", child_report.get("legacyDetailOpens") is True)
        record(checks, "legacy workflow progress opens read-only", child_report.get("legacyProgressOpens") is True)
        record(checks, "legacy workflow is not rewritten", child_report.get("legacyUnchanged") is True)
        record(checks, "legacy session source remains in place", child_report.get("legacySessionExists") is True)
        record(checks, "A notes remain after B work", child_report.get("notesUnchanged") is True)

    except Exception as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
        summary["traceback"] = traceback.format_exc()
        record(checks, "E2E driver raised no exception", False, summary["error"])
    finally:
        if bridge is not None:
            shutdown_bridge(bridge)
        summary["elapsedSeconds"] = round(time.time() - started, 1)
        summary["passed"] = bool(checks) and all(check["ok"] for check in checks)
        summary["eventCounts"] = {
            event_type: len(rec.of(event_type))
            for event_type in sorted({str(event.get("type")) for event in rec.events})
        }
        write_bundle(bundle, rec, summary)
        log("")
        log("Bundle: %s" % bundle)
        log("Result: %s" % ("PASS" if summary["passed"] else "FAIL"))
    return 0 if summary.get("passed") else 1


if __name__ == "__main__":
    raise SystemExit(run())
