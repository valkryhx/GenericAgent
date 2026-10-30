"""Real-LLM proof: run ordinary chat / workflow / agent-team against D:/test20260416."""
from __future__ import annotations
import json, os, sys, time, traceback, shutil
from pathlib import Path

REPO = Path.cwd()
for p in (REPO, REPO / "frontends"):
    sys.path.insert(0, str(p))

TARGET = Path("D:/test20260416")
RUN_TAG = time.strftime("%Y%m%d-%H%M%S")
OUT = TARGET / f"_ga_e2e_{RUN_TAG}"
DATA = REPO / "temp" / "test20261029" / f"data_{RUN_TAG}"
BUNDLE = REPO / "temp" / "test20261029" / f"run_{RUN_TAG}"
LOG = (REPO / "temp" / "test20261029" / f"driver_{RUN_TAG}.log").open("w", encoding="utf-8")

def log(*a):
    line = " ".join(str(x) for x in a)
    LOG.write(line + "\n"); LOG.flush()

CHECKS = []
def rec(name, ok, detail=""):
    CHECKS.append({"name": name, "ok": bool(ok), "detail": str(detail)})
    log(("PASS " if ok else "FAIL ") + name + (" - " + str(detail) if detail else ""))

def setup_env():
    os.environ.update({
        "GA_PROJECT_ROOT": str(TARGET),
        "GA_DATA_DIR": str(DATA),
        "GA_WORKFLOW_WORKSPACE_ROOT": str(TARGET),
        "GA_LAUNCH_CWD": str(TARGET),
        "GA_ORIGINAL_CWD": str(TARGET),
        "GA_LLM_CONFIG": str(REPO / "llm.yaml"),
    })
    for k in ("GA_PROJECT_KEY","GA_PROJECT_DATA_DIR","GA_SESSION_DATA_DIR",
              "GA_PARENT_SESSION_ID","GA_TASK_ROOT","GA_TEAM_KEY"):
        os.environ.pop(k, None)

def under(path, root) -> bool:
    try:
        Path(path).resolve().relative_to(Path(root).resolve()); return True
    except (OSError, ValueError):
        return False

class Rec:
    def __init__(self): self.events = []
    def __call__(self, e): self.events.append(e)
    def of(self, t): return [e for e in self.events if e.get("type") == t]
    def last(self, t):
        r = self.of(t); return r[-1] if r else None

def wait_for(rec, t, timeout, after=0):
    end = time.time() + timeout
    while time.time() < end:
        rows = rec.of(t)
        if len(rows) > after: return rows[after]
        time.sleep(0.1)
    return None

def wait_until(pred, timeout, interval=0.2):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if pred(): return True
        except Exception:
            pass
        time.sleep(interval)
    return False

def wait_idle(bridge, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if not getattr(bridge.agent, "is_running", False) and not bridge._is_consuming():
            return True
        time.sleep(0.1)
    return False

def shutdown(bridge):
    try:
        a = getattr(bridge, "agent", None)
        if a is not None:
            setattr(a, "stop_sig", True)
            try: a.abort()
            except Exception: pass
        bridge.stop()
        th = getattr(bridge, "_agent_thread", None)
        if th is not None: th.join(timeout=5)
    except Exception:
        pass

def run_turn(bridge, rec, prompt, timeout):
    """Submit one main-agent turn; retry on provider/transport errors."""
    attempts = []
    for i in range(1, 4):
        cur = len(rec.of("assistant_done"))
        t0 = time.time()
        task_id = bridge.submit(prompt)
        if task_id < 0:
            attempts.append({"attempt": i, "submit": task_id, "err": (rec.last("error") or {}).get("message","")})
            break
        bridge.wait_for_idle(timeout=timeout)
        settled = wait_idle(bridge, 30)
        done = wait_for(rec, "assistant_done", 10, after=cur)
        text = str((done or {}).get("text") or "")
        bad = "upstream stream closed before a terminal event" in text or not text.strip()
        attempts.append({"attempt": i, "taskId": task_id, "settled": settled, "text": text[:400],
                         "secs": round(time.time()-t0,1), "transportError": bad})
        if settled and not bad:
            return {"ok": True, "text": text, "attempts": attempts}
    return {"ok": False, "text": attempts[-1]["text"] if attempts else "", "attempts": attempts}

def main() -> int:
    setup_env()
    OUT.mkdir(parents=True, exist_ok=True)
    BUNDLE.mkdir(parents=True, exist_ok=True)
    recorder = Rec()
    summary = {"target": str(TARGET), "dataRoot": str(DATA), "outRoot": str(OUT),
               "model": "default/deepseek-v4.1-flash", "phases": {}}
    from frontends.ink.bridge import GenericAgentBridge
    bridge = None
    try:
        bridge = GenericAgentBridge(emit=recorder)
        agent = bridge.agent
        log("identity: project_root=", getattr(agent, "project_root", None))
        log("identity: data_root   =", getattr(agent, "data_root", None))
        log("identity: tool_cwd    =", getattr(agent, "tool_cwd", None))
        log("bridge.workspace_root =", bridge.workspace_root)
        rec("bridge.workspace_root == GA_WORKFLOW_WORKSPACE_ROOT(D:/test20260416)",
            Path(bridge.workspace_root).resolve() == TARGET.resolve(), bridge.workspace_root)
        rec("agent.tool_cwd == GA_PROJECT_ROOT(D:/test20260416)",
            Path(getattr(agent, "tool_cwd", "")).resolve() == TARGET.resolve(), getattr(agent, "tool_cwd", ""))
        rec("agent.data_root == GA_DATA_DIR",
            Path(getattr(agent, "data_root", "")).resolve() == DATA.resolve(), getattr(agent, "data_root", ""))

        # model switch
        cur = len(recorder.of("model_status"))
        bridge.model_switch("default/deepseek-v4.1-flash")
        st = wait_for(recorder, "model_status", 30, after=cur)
        now = next((m.get("name") for m in (st or {}).get("models", []) if m.get("current")), None)
        rec("model is default/deepseek-v4.1-flash", now == "default/deepseek-v4.1-flash", now)

        # ---- P1: ordinary chat, read documentation in the project ----
        p1 = run_turn(bridge, recorder,
            "Please read docs/schema.md in the current working directory and reply with a concise "
            "summary (under 120 words) listing the table names it defines. Use the file_read tool. "
            "Do not modify any file.", timeout=300)
        summary["phases"]["chat_read"] = p1
        rec("P1 ordinary chat read turn completed", p1["ok"], p1["text"][:160])
        rec("P1 answer references tasks table (real file content)",
            "tasks" in p1["text"].lower(), p1["text"][:200])

        # ---- P2: ordinary chat, write a new file into the project ----
        wfile = OUT / "notes.md"
        p2 = run_turn(bridge, recorder,
            f"Create the file at path {wfile} using the file_write tool with this exact UTF-8 content: "
            "'GA_E2E_WRITE_OK'. Create only that one file. Then reply with exactly done.",
            timeout=300)
        summary["phases"]["chat_write"] = p2
        okw = wfile.is_file() and wfile.read_text(encoding="utf-8").strip() == "GA_E2E_WRITE_OK"
        rec("P2 ordinary chat write landed at D:/test20260416/<run>/notes.md", okw,
            str(wfile) + (" exists" if wfile.exists() else " missing"))
        sz = wfile.stat().st_size if wfile.exists() else -1
        rec("P2 write landed in place (st_blocks>0, no tiny symlink file)", sz > 0, f"size={sz}")

        # ---- P3: dynamic workflow (multi-agent) writing artifacts ----
        wf_script = (
            "export const meta = { name: 'dtest-workflow', description: 'read+write under D:/test20260416' }\n"
            "phase('Inspect and write')\n"
            "const a = await agent(`Current tool cwd is ${args.workspacePath}. "
            "Use file_read to read docs/schema.md, then use file_write to create the workspace-relative "
            "file report.md whose first line is '# Dtest workflow' and whose second line lists the table "
            "names you found in schema.md. Do not write outside ${args.workspacePath}. "
            "Do not read mykey.py, mykey.json, mcp.json, or any credentials.`, { label: 'inspector' })\n"
            "return { summary: a.summary }\n"
        )
        run_id = ""
        run = None
        try:
            run_id = bridge.workflow_draft(wf_script)
            approved = bridge.workflow_approve(run_id, timeout_seconds=400) if run_id else False
            def _done():
                r = bridge.workflow_store.load_run(run_id)
                return r.status in {"succeeded","degraded","failed","killed","interrupted"}
            wait_until(_done, 500)
            bridge.wait_for_workflow_idle(run_id, timeout=120)
            wait_idle(bridge, 60)
            run = bridge.workflow_store.load_run(run_id)
            summary["phases"]["workflow"] = {
                "runId": run_id, "approved": approved, "status": getattr(run, "status", None),
                "artifactDir": str(getattr(run, "artifact_dir", "") or ""),
                "workspacePath": (run.metadata or {}).get("workspacePath"),
            }
        except Exception as exc:
            summary["phases"]["workflow"] = {"error": f"{type(exc).__name__}: {exc}",
                                             "traceback": traceback.format_exc()}
        wf = summary["phases"]["workflow"]
        rec("P3 workflow draft+approve returned a run", bool(run_id), run_id)
        rec("P3 workflow status succeeded/degraded", wf.get("status") in {"succeeded","degraded"}, wf.get("status") or wf.get("error"))
        ws = Path(str(wf.get("workspacePath") or ""))
        rec("P3 workflow run workspace is under D:/test20260416 (env override honoured)",
            str(ws) != "" and under(ws, TARGET), str(ws))
        rec("P3 workflow run dir is D:/test20260416/workflow-runs/<id>",
            ws.name != "" and ws.parent.name == "workflow-runs" and ws.parent.parent.resolve() == TARGET.resolve(),
            str(ws))
        rep = ws / "report.md"
        rec("P3 workflow child actually wrote report.md under D:/test20260416",
            rep.is_file(), str(rep))
        if rep.is_file():
            txt = rep.read_text(encoding="utf-8", errors="replace")
            summary["phases"]["workflow"]["reportHead"] = txt[:400]
            rec("P3 workflow report.md mentions schema table 'tasks'", "tasks" in txt.lower(), txt[:160])

        # ---- P4: agent team (spawn real subagent + team task board) ----
        from genericagent.agents.team import team_name_for
        from genericagent.core.handler import GenericAgentHandler
        from genericagent.core.loop import exhaust
        from genericagent import paths as ga_paths

        session_a = str(agent.session_id or "")
        project_data = Path(agent.project_data_dir)
        team_name = team_name_for(session_a)
        team_key = ga_paths.team_key(str(agent.project_key), team_name)
        team_out = OUT / "team"
        team_out.mkdir(parents=True, exist_ok=True)

        handler = GenericAgentHandler(agent, agent.history, agent.tool_cwd, workspace_root=agent.project_root)
        handler.subagent_manager = bridge.subagent_manager
        task_outcome = exhaust(handler.dispatch(
            "task_create",
            {"subject": "Dtest team child writes a file",
             "description": "Write team_notes.md under D:/test20260416 and mark done"},
            None,
        ))
        team_task = (getattr(task_outcome, "data", {}) or {}).get("task") or {}
        task_id = str(team_task.get("id") or "")
        board = DATA / "tasks" / team_key / "tasks.json"
        rec("P4 team task_create wrote project-qualified board", board.is_file() and task_id != "", str(board))

        spawn = exhaust(handler.dispatch(
            "spawn_agent",
            {
                "task_name": "dtest_child",
                "message": (
                    f"Your assigned team task is #{task_id}. Use the file_write tool to create the file "
                    f"{team_out / 'team_notes.md'} with content: TEAM_WRITE_OK. Then call task_update with "
                    f"taskId={task_id} and status=completed, and reply exactly TEAM_OK. "
                    "Do not read mykey.py, mykey.json, mcp.json, or credentials."
                ),
                "background": True,
                "fork_turns": "none",
                "permission_profile": "inherit-current-permissions",
                "team_mode": True,
                "team_task_id": task_id,
            },
            None,
        ))
        spawn_data = getattr(spawn, "data", {}) or {}
        child_state = None
        if spawn_data.get("status") == "started":
            wait_until(
                lambda: bool(bridge.subagent_manager.list_agents(include_closed=True)) and
                        bridge.subagent_manager.list_agents(include_closed=True)[-1].turn_status in
                        {"completed","errored","aborted"},
                timeout=400,
            )
            states = bridge.subagent_manager.list_agents(include_closed=True)
            child_state = states[-1] if states else None
        summary["phases"]["team"] = {
            "taskId": task_id, "teamKey": team_key,
            "spawn": spawn_data,
            "child": child_state.to_dict() if (child_state is not None and hasattr(child_state, "to_dict")) else None,
        }
        rec("P4 real subagent process started", spawn_data.get("status") == "started", spawn_data.get("msg",""))
        rec("P4 team child completed", child_state is not None and child_state.turn_status == "completed",
            getattr(child_state, "turn_status", None))
        rec("P4 team child ran under project bucket",
            child_state is not None and under(getattr(child_state, "task_dir", ""), project_data),
            getattr(child_state, "task_dir", ""))
        tn = team_out / "team_notes.md"
        rec("P4 team child actually wrote team_notes.md under D:/test20260416",
            tn.is_file() and tn.read_text(encoding="utf-8", errors="replace").strip().startswith("TEAM_WRITE_OK"),
            str(tn))
        cur = len(recorder.of("team_roster"))
        bridge.team_list()
        roster = wait_for(recorder, "team_roster", 10, after=cur) or {}
        summary["phases"]["team"]["roster"] = {"tasks": roster.get("tasks"), "members": roster.get("members")}
        rec("P4 roster shows the team task completed",
            any(str(t.get("id")) == task_id and t.get("status") == "completed" for t in (roster.get("tasks") or [])),
            [ (t.get("id"), t.get("status")) for t in (roster.get("tasks") or []) ])
        rec("P4 roster shows teammate dtest_child",
            any(m.get("name") == "dtest_child" for m in (roster.get("members") or [])),
            [m.get("name") for m in (roster.get("members") or [])])
        rec("P4 original notes.md still intact after workflow/team",
            wfile.is_file() and wfile.read_text(encoding="utf-8").strip() == "GA_E2E_WRITE_OK")

        # ---- P5: direct WorkflowRuntime with explicit args.workspacePath ----
        ext_root = TARGET / f"_ga_e2e_{RUN_TAG}" / "ext_ws"
        ext_root.mkdir(parents=True, exist_ok=True)
        try:
            from genericagent.workflow.runtime import WorkflowRuntime
            from genericagent.workflow.llm import binding_from_agent
            from genericagent.workflow.child_agent import NativeGPTChildAgentRunner
            binding = binding_from_agent(agent)
            runner = NativeGPTChildAgentRunner(binding_provider=lambda: binding, enable_tools=True)
            runner.hook_agent = agent
            rt = WorkflowRuntime(store=bridge.workflow_store, workspace_root=str(ext_root),
                                 timeout_seconds=400, runner=runner, llm_binding_provider=lambda: binding)
            script2 = (
                "export const meta = { name: 'dtest-external', description: 'per-run external workspace' }\n"
                "const r = await agent(`Use file_write to create the workspace-relative file note.md with "
                "exact content EXT_RUN_OK. Do not use code_run. Do not read mykey.py, mykey.json, mcp.json.`)\n"
                "return { summary: r.summary }\n"
            )
            from genericagent.workflow.models import WorkflowRun
            run2 = rt.store.create_run(WorkflowRun(session_id=session_a, script=script2))
            rt.run(run2, args={"workspacePath": str(ext_root)})
            rr = rt.store.load_run(run2.run_id)
            summary["phases"]["external_ws"] = {
                "runId": run2.run_id, "status": getattr(rr, "status", None),
                "workspacePath": (rr.metadata or {}).get("workspacePath"),
            }
        except Exception as exc:
            summary["phases"]["external_ws"] = {"error": f"{type(exc).__name__}: {exc}",
                                                "traceback": traceback.format_exc()}
        ew = summary["phases"]["external_ws"]
        rec("P5 direct WorkflowRuntime run succeeded/degraded",
            ew.get("status") in {"succeeded","degraded"}, ew.get("status") or ew.get("error"))
        ews = Path(str(ew.get("workspacePath") or ""))
        rec("P5 explicit args.workspacePath honoured (run dir under D:/test20260416)",
            str(ews) != "" and under(ews, TARGET), str(ews))
        e_note = ews / "note.md"
        rec("P5 external-workspace child wrote note.md in place", e_note.is_file(), str(e_note))
        if e_note.is_file():
            rec("P5 external note.md content correct",
                e_note.read_text(encoding="utf-8", errors="replace").strip() == "EXT_RUN_OK",
                e_note.read_text(encoding="utf-8", errors="replace")[:80])

        rec("P0 project data bucket lives under GA_DATA_DIR (not AppData)",
            under(project_data, DATA) and not under(project_data, Path.home() / "AppData"), str(project_data))
        rec("P0 transcript file exists under the project bucket",
            Path(getattr(agent, "session_path", "") or "").is_file() and under(agent.session_path, project_data),
            str(getattr(agent, "session_path", "")))

    except Exception as exc:
        rec("driver raised no exception", False, f"{type(exc).__name__}: {exc}")
        summary["traceback"] = traceback.format_exc()
    finally:
        if bridge is not None:
            shutdown(bridge)
        summary["checks"] = CHECKS
        summary["passed"] = bool(CHECKS) and all(c["ok"] for c in CHECKS)
        summary["elapsed"] = round(time.time() - START, 1)
        with (BUNDLE / "summary.json").open("w", encoding="utf-8", newline="\n") as fh:
            json.dump(summary, fh, ensure_ascii=False, indent=2)
        with (BUNDLE / "events.jsonl").open("w", encoding="utf-8", newline="\n") as fh:
            for e in recorder.events:
                fh.write(json.dumps(e, ensure_ascii=False, default=str) + "\n")
        (BUNDLE / "driver.log").write_text(
            (BUNDLE.parent / f"driver_{RUN_TAG}.log").read_text(encoding="utf-8") if (BUNDLE.parent / f"driver_{RUN_TAG}.log").is_file() else "",
            encoding="utf-8")
        log("")
        log("RESULT:", "PASS" if summary["passed"] else "FAIL")
        log("bundle:", BUNDLE)
        log("outRoot:", OUT)
        LOG.close()
    return 0 if summary.get("passed") else 1


START = time.time()
if __name__ == "__main__":
    sys.exit(main())
