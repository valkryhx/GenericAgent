"""Real-LLM proof against D:/test20260416/123 (empty dir).

Covers: ordinary chat read/write, code generation, UTF-8 Chinese files,
subdirectory creation with nested read/write, dynamic workflow, agent team.
Artifacts are KEPT for the user to inspect (no cleanup of target).
"""
from __future__ import annotations
import json, os, sys, time, traceback
from pathlib import Path

REPO = Path.cwd()
for p in (REPO, REPO / "frontends"):
    sys.path.insert(0, str(p))

TARGET = Path(r"D:/test20260416/123")
RUN_TAG = time.strftime("%Y%m%d-%H%M%S")
DATA = REPO / "temp" / "dtest123" / f"data_{RUN_TAG}"
BUNDLE = REPO / "temp" / "dtest123" / f"run_{RUN_TAG}"
BUNDLE.mkdir(parents=True, exist_ok=True)
LOG = (BUNDLE / "driver.log").open("w", encoding="utf-8")

def log(*a):
    LOG.write(" ".join(str(x) for x in a) + "\n"); LOG.flush()

CHECKS = []
def rec(name, ok, detail=""):
    CHECKS.append({"name": name, "ok": bool(ok), "detail": str(detail)})
    log(("PASS " if ok else "FAIL ") + name + (" - " + str(detail) if detail else ""))

def rel(p): return str(Path(p).relative_to(TARGET)).replace(os.sep, "/")

def file_info(p):
    p = Path(p)
    if not p.is_file(): return {"exists": False}
    b = p.read_bytes()
    try:
        txt = b.decode("utf-8"); enc = "utf-8"
    except UnicodeDecodeError:
        txt = b.decode("utf-8", "replace"); enc = "non-utf8"
    return {"exists": True, "size": len(b), "encoding": enc, "has_bom": b[:3] == b"\xef\xbb\xbf", "text": txt}

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

class Rec:
    def __init__(self): self.events = []
    def __call__(self, e): self.events.append(e)
    def of(self, t): return [e for e in self.events if e.get("type") == t]
    def last(self, t):
        r = self.of(t); return r[-1] if r else None

def wait_for(recorder, t, timeout, after=0):
    end = time.time() + timeout
    while time.time() < end:
        rows = recorder.of(t)
        if len(rows) > after: return rows[after]
        time.sleep(0.1)
    return None

def wait_until(pred, timeout, interval=0.2):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if pred(): return True
        except Exception: pass
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
    except Exception: pass

def run_turn(bridge, recorder, prompt, timeout):
    attempts = []
    for i in range(1, 4):
        cur = len(recorder.of("assistant_done"))
        t0 = time.time()
        task_id = bridge.submit(prompt)
        if task_id < 0:
            attempts.append({"attempt": i, "submit": task_id, "err": (recorder.last("error") or {}).get("message","")})
            break
        bridge.wait_for_idle(timeout=timeout)
        settled = wait_idle(bridge, 30)
        done = wait_for(recorder, "assistant_done", 10, after=cur)
        text = str((done or {}).get("text") or "")
        bad = "upstream stream closed before a terminal event" in text or not text.strip()
        attempts.append({"attempt": i, "taskId": task_id, "settled": settled,
                         "text": text[-1500:], "secs": round(time.time()-t0,1), "transportError": bad})
        if settled and not bad:
            return {"ok": True, "text": text, "attempts": attempts}
    return {"ok": False, "text": attempts[-1]["text"] if attempts else "", "attempts": attempts}

def main() -> int:
    setup_env()
    recorder = Rec()
    summary = {"target": str(TARGET), "dataRoot": str(DATA),
               "model": "default/deepseek-v4.1-flash", "phases": {}}
    from frontends.ink.bridge import GenericAgentBridge
    bridge = None
    try:
        bridge = GenericAgentBridge(emit=recorder)
        agent = bridge.agent
        log("project_root=", getattr(agent,"project_root",None))
        log("data_root   =", getattr(agent,"data_root",None))
        log("tool_cwd    =", getattr(agent,"tool_cwd",None))
        log("workspace   =", bridge.workspace_root)
        rec("identity: tool_cwd == D:/test20260416/123", Path(agent.tool_cwd).resolve()==TARGET.resolve(), agent.tool_cwd)
        rec("identity: workspace_root == D:/test20260416/123", Path(bridge.workspace_root).resolve()==TARGET.resolve(), bridge.workspace_root)

        cur = len(recorder.of("model_status"))
        bridge.model_switch("default/deepseek-v4.1-flash")
        st = wait_for(recorder, "model_status", 30, after=cur)
        now = next((m.get("name") for m in (st or {}).get("models",[]) if m.get("current")), None)
        rec("model is default/deepseek-v4.1-flash", now=="default/deepseek-v4.1-flash", now)

        # ---------- A1: code generation (main agent writes a Python module) ----------
        calc = TARGET / "src" / "calc.py"
        p = run_turn(bridge, recorder,
            f"Create the file {calc} (create the src/ subdirectory as needed) using file_write. "
            "It must be a Python 3 module defining: add(a,b), sub(a,b), mul(a,b), div(a,b) where div raises "
            "ZeroDivisionError when b==0. Pure ASCII, no external imports. Reply with just done.", timeout=300)
        summary["phases"]["A1_codegen"] = p
        fi = file_info(calc)
        rec("A1 code file created in src/ subdir", fi["exists"], str(calc))
        if fi["exists"]:
            import ast
            ok_syntax = True
            try: ast.parse(fi["text"])
            except SyntaxError as e: ok_syntax = False; fi["syntax_error"]=str(e)
            rec("A1 generated code is valid Python (ast.parse)", ok_syntax)
            for fn in ("def add(", "def sub(", "def mul(", "def div("):
                rec(f"A1 defines {fn.strip()}", fn in fi["text"])
            rec("A1 UTF-8 decodes cleanly", fi["encoding"]=="utf-8", fi["encoding"])
            summary["phases"]["A1_codegen"]["file"] = fi

        # ---------- A2: UTF-8 Chinese file ----------
        zh = TARGET / "docs" / "说明.md"
        p = run_turn(bridge, recorder,
            f"Create the file {zh} (create the docs/ subdirectory). Content must be exactly these three "
            "lines and nothing else: '第一行：中文编码测试' / '第二行：emoji ✅ 与符号 → ≤ ≥' / '第三行：结束' "
            "(each line separated by a newline, UTF-8). Use file_write. Reply with just done.", timeout=300)
        summary["phases"]["A2_chinese"] = p
        fi = file_info(zh)
        rec("A2 Chinese file created in docs/ subdir", fi["exists"], str(zh))
        if fi["exists"]:
            rec("A2 correct UTF-8 (no BOM, decodes)", fi["encoding"]=="utf-8" and not fi["has_bom"], fi["encoding"])
            rec("A2 line1 exact", "第一行：中文编码测试" in fi["text"], fi["text"][:60])
            rec("A2 line2 has emoji+symbols", "emoji ✅" in fi["text"] and "≤ ≥" in fi["text"])
            rec("A2 line3 exact", "第三行：结束" in fi["text"])
            summary["phases"]["A2_chinese"]["file"] = {k: fi[k] for k in ("size","encoding","has_bom","text")}

        # ---------- A3: nested subdirectory read/write via code_run ----------
        nested = TARGET / "data" / "raw" / "sample.txt"
        p = run_turn(bridge, recorder,
            "Use code_run (python) to: (1) create the directory structure data/raw/nested/ under the current "
            "working directory, (2) write data/raw/sample.txt with three lines of integers 10/20/30, "
            "(3) read the file back, compute the sum, and (4) write the result integer to "
            "data/raw/nested/result.txt. Print the sum. Do not use file_write for this task.", timeout=300)
        summary["phases"]["A3_nested"] = p
        s1 = TARGET / "data" / "raw" / "sample.txt"
        r1 = TARGET / "data" / "raw" / "nested" / "result.txt"
        rec("A3 nested dir+sample.txt created via code_run", s1.is_file(), str(s1))
        rec("A3 nested/result.txt created", r1.is_file(), str(r1))
        if r1.is_file():
            got = r1.read_text(encoding="utf-8", errors="replace").strip()
            rec("A3 code_run computed correct sum (10+20+30=60)", got == "60", repr(got))
        rec("A3 answer reports the sum 60", "60" in p["text"], p["text"][-200:])

        # ---------- A4: main agent reads back its own generated files ----------
        p = run_turn(bridge, recorder,
            "Read the files src/calc.py and docs/说明.md using file_read, then answer with a single line "
            "containing: the number of function definitions in calc.py as an integer, and the first line of "
            "说明.md verbatim, separated by ' | '.", timeout=300)
        summary["phases"]["A4_readback"] = p
        rec("A4 readback mentions 4 functions", "4" in p["text"], p["text"][-300:])
        rec("A4 readback quotes Chinese first line", "第一行：中文编码测试" in p["text"], p["text"][-200:])

        # ---------- B1: workflow with two children doing nested generation ----------
        wf = (
            "export const meta = { name: 'dtest123-wf', description: 'nested generation + readback' }\n"
            "phase('Build and verify')\n"
            "const a = await agent(`Current tool cwd is ${args.workspacePath}. Use code_run (python) to create the "
            "directory reports/q1/ under the current working directory and write reports/q1/summary.md with the "
            "content lines '# Q1 Summary' and 'Quarter one revenue summary generated by a workflow child.', then "
            "read it back and report its first line. Do not write outside ${args.workspacePath}.`, { label: 'builder' })\n"
            "const b = await agent(`Current tool cwd is ${args.workspacePath}. Use file_read to read the file "
            "reports/q1/summary.md and reply with its exact second line. Do not write any file.`, { label: 'checker' })\n"
            "return { built: a.summary, checked: b.summary }\n"
        )
        run_id = ""
        wfsum = {}
        try:
            run_id = bridge.workflow_draft(wf)
            approved = bridge.workflow_approve(run_id, timeout_seconds=500) if run_id else False
            wait_until(lambda: bridge.workflow_store.load_run(run_id).status in
                       {"succeeded","degraded","failed","killed","interrupted"}, 600)
            bridge.wait_for_workflow_idle(run_id, timeout=120)
            wait_idle(bridge, 60)
            run = bridge.workflow_store.load_run(run_id)
            artifact_dir = str(getattr(run, "artifact_dir", "") or "")
            wfsum = {"runId": run_id, "approved": approved, "status": getattr(run,"status",None),
                     "artifactDir": artifact_dir,
                     "workspacePath": (run.metadata or {}).get("workspacePath")}
        except Exception as exc:
            wfsum = {"error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()}
        summary["phases"]["B1_workflow"] = wfsum
        rec("B1 workflow returned a run", bool(run_id), run_id)
        rec("B1 workflow succeeded/degraded", wfsum.get("status") in {"succeeded","degraded"}, wfsum.get("status") or wfsum.get("error"))
        ws = Path(str(wfsum.get("workspacePath") or ""))
        rec("B1 run workspace under D:/test20260416/123", str(ws)!="" and str(ws).startswith(str(TARGET.resolve())), str(ws))
        rec("B1 run dir is <target>/workflow-runs/<id>", ws.parent.name=="workflow-runs" and ws.parent.parent.resolve()==TARGET.resolve(), str(ws))
        sm = ws / "reports" / "q1" / "summary.md"
        rec("B1 workflow child created nested reports/q1/summary.md", sm.is_file(), str(sm))
        if sm.is_file():
            t = sm.read_text(encoding="utf-8", errors="replace")
            rec("B1 summary.md first line correct", t.splitlines()[0].strip()=="# Q1 Summary", t[:120])
            summary["phases"]["B1_workflow"]["summaryFile"] = t[:500]
        jr = Path(wfsum.get("artifactDir") or "") / "journal.jsonl"
        rec("B1 run has journal.jsonl in artifact dir", jr.is_file(), str(jr))
        if jr.is_file():
            labels = [ln for ln in jr.read_text(encoding="utf-8",errors="replace").splitlines() if "builder" in ln or "checker" in ln]
            rec("B1 journal shows builder & checker jobs", len(labels) >= 2, f"{len(labels)} refs")

        # ---------- B2: agent team child does nested generation ----------
        from genericagent.agents.team import team_name_for
        from genericagent.core.handler import GenericAgentHandler
        from genericagent.core.loop import exhaust
        from genericagent import paths as ga_paths

        session = str(agent.session_id or "")
        project_data = Path(agent.project_data_dir)
        team_name = team_name_for(session)
        team_key = ga_paths.team_key(str(agent.project_key), team_name)
        team_dir = TARGET / "team_out"
        team_dir.mkdir(parents=True, exist_ok=True)
        handler = GenericAgentHandler(agent, agent.history, agent.tool_cwd, workspace_root=agent.project_root)
        handler.subagent_manager = bridge.subagent_manager
        to = exhaust(handler.dispatch("task_create",
            {"subject": "Team child generates nested Chinese file", "description": "write under D:/test20260416/123"}, None))
        task_id = str(((getattr(to,"data",{}) or {}).get("task") or {}).get("id") or "")
        rec("B2 team task_create wrote board", (DATA/"tasks"/team_key/"tasks.json").is_file() and task_id!="", task_id)

        child_target = team_dir / "sub" / "notes.txt"
        spawn = exhaust(handler.dispatch("spawn_agent", {
            "task_name": "dtest123_child",
            "message": (
                f"Your assigned team task is #{task_id}. Use code_run (python) to create the directory "
                f"{team_dir/'sub'} and write the file {child_target} with the exact UTF-8 content line "
                f"'子代理写入：团队协作测试成功'. Then use file_read to read that file back. Then call "
                f"task_update with taskId={task_id} and status=completed, and reply exactly TEAM123_OK. "
                "Do not write anywhere except under D:/test20260416/123."
            ),
            "background": True, "fork_turns": "none",
            "permission_profile": "inherit-current-permissions",
            "team_mode": True, "team_task_id": task_id,
        }, None))
        sd = getattr(spawn,"data",{}) or {}
        child_state = None
        if sd.get("status") == "started":
            wait_until(lambda: bool(bridge.subagent_manager.list_agents(include_closed=True)) and
                       bridge.subagent_manager.list_agents(include_closed=True)[-1].turn_status in
                       {"completed","errored","aborted"}, 500)
            st = bridge.subagent_manager.list_agents(include_closed=True)
            child_state = st[-1] if st else None
        summary["phases"]["B2_team"] = {"taskId": task_id, "teamKey": team_key, "spawn": sd,
            "child": child_state.to_dict() if (child_state is not None and hasattr(child_state,"to_dict")) else None}
        rec("B2 subagent started", sd.get("status")=="started", sd.get("msg",""))
        rec("B2 subagent completed", child_state is not None and child_state.turn_status=="completed",
            getattr(child_state,"turn_status",None))
        rec("B2 subagent state lives in project bucket", child_state is not None and
            str(getattr(child_state,"task_dir","")).startswith(str(project_data)), getattr(child_state,"task_dir",""))
        cf = file_info(child_target)
        rec("B2 subagent created nested sub/notes.txt in D:/test20260416/123", cf["exists"], str(child_target))
        if cf["exists"]:
            rec("B2 subagent Chinese content correct", "子代理写入：团队协作测试成功" in cf["text"], cf["text"][:80])
            summary["phases"]["B2_team"]["notesFile"] = cf["text"]
        cur = len(recorder.of("team_roster")); bridge.team_list()
        roster = wait_for(recorder,"team_roster",10,after=cur) or {}
        rec("B2 roster task completed", any(str(t.get("id"))==task_id and t.get("status")=="completed" for t in (roster.get("tasks") or [])),
            [(t.get("id"),t.get("status")) for t in (roster.get("tasks") or [])])
        rec("B2 roster shows dtest123_child", any(m.get("name")=="dtest123_child" for m in (roster.get("members") or [])),
            [m.get("name") for m in (roster.get("members") or [])])

        # ---------- C1: direct WorkflowRuntime with explicit subdir workspace ----------
        ext = TARGET / "ext_ws"
        ext.mkdir(parents=True, exist_ok=True)
        csum = {}
        try:
            from genericagent.workflow.runtime import WorkflowRuntime
            from genericagent.workflow.models import WorkflowRun
            from genericagent.workflow.llm import binding_from_agent
            from genericagent.workflow.child_agent import NativeGPTChildAgentRunner
            binding = binding_from_agent(agent)
            runner = NativeGPTChildAgentRunner(binding_provider=lambda: binding, enable_tools=True)
            runner.hook_agent = agent
            rt = WorkflowRuntime(store=bridge.workflow_store, workspace_root=str(ext),
                                 timeout_seconds=400, runner=runner, llm_binding_provider=lambda: binding)
            s2 = ("export const meta = { name: 'dtest123-ext', description: 'explicit subdir workspace' }\n"
                  "const r = await agent(`Use code_run to create the directory out/ and write out/hello.txt with "
                  "content EXT123_OK, then read it back. Do not write outside ${args.workspacePath}.`)\n"
                  "return { summary: r.summary }\n")
            run2 = rt.store.create_run(WorkflowRun(session_id=session, script=s2))
            rt.run(run2, args={"workspacePath": str(ext)})
            rr = rt.store.load_run(run2.run_id)
            csum = {"runId": run2.run_id, "status": getattr(rr,"status",None),
                    "workspacePath": (rr.metadata or {}).get("workspacePath")}
        except Exception as exc:
            csum = {"error": f"{type(exc).__name__}: {exc}", "traceback": traceback.format_exc()}
        summary["phases"]["C1_external"] = csum
        rec("C1 direct WorkflowRuntime succeeded/degraded", csum.get("status") in {"succeeded","degraded"}, csum.get("status") or csum.get("error"))
        ews = Path(str(csum.get("workspacePath") or ""))
        rec("C1 explicit args.workspacePath honoured under target", str(ews)!="" and str(ews).startswith(str(TARGET.resolve())), str(ews))
        hf = ews / "out" / "hello.txt"
        rec("C1 child created out/hello.txt", hf.is_file(), str(hf))
        if hf.is_file():
            rec("C1 hello.txt content EXT123_OK", hf.read_text(encoding="utf-8",errors="replace").strip()=="EXT123_OK")

        # ---------- D1: whole-tree inventory + manifest ----------
        files = []
        for dp, dn, fn in os.walk(TARGET):
            for f in fn:
                if f == "_MANIFEST.txt":
                    continue
                fp = Path(dp)/f
                files.append(fp)
        files.sort()
        manifest = TARGET / "_MANIFEST.txt"
        lines = ["GA E2E output manifest (%s)" % RUN_TAG, "model: default/deepseek-v4.1-flash", ""]
        for fp in files:
            b = fp.read_bytes()
            enc = "utf-8"
            try: b.decode("utf-8")
            except UnicodeDecodeError: enc = "non-utf8"
            lines.append(f"{rel(fp)}\t{b'BOM' if b[:3]==b'\xef\xbb\xbf' else 'noBOM'}\t{enc}\t{len(b)}B")
        manifest.write_text("\n".join(lines)+"\n", encoding="utf-8", newline="\n")
        rec("D1 expected key artifacts all present",
            all((TARGET/p).exists() for p in ["src/calc.py","docs/说明.md","data/raw/sample.txt",
                "data/raw/nested/result.txt","team_out/sub/notes.txt"]),
            f"{len(files)} files")
        summary["inventory"] = [rel(p) for p in files]
        summary["topLevel"] = sorted(os.listdir(TARGET))
        rec("D1 no GA runtime dirs polluted target top-level",
            not any((TARGET/d).exists() for d in ["assets","skills","memory"]) and
            all(x not in os.listdir(TARGET) for x in ["assets","skills","memory"]),
            str(os.listdir(TARGET)))
    except Exception as exc:
        rec("driver raised no exception", False, f"{type(exc).__name__}: {exc}")
        summary["traceback"] = traceback.format_exc()
    finally:
        if bridge is not None: shutdown(bridge)
        summary["checks"] = CHECKS
        summary["passed"] = bool(CHECKS) and all(c["ok"] for c in CHECKS)
        summary["elapsed"] = round(time.time()-START,1)
        (BUNDLE/"summary.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8",newline="\n")
        with (BUNDLE/"events.jsonl").open("w",encoding="utf-8",newline="\n") as fh:
            for e in recorder.events:
                fh.write(json.dumps(e,ensure_ascii=False,default=str)+"\n")
        log(""); log("RESULT:", "PASS" if summary["passed"] else "FAIL"); log("bundle:", BUNDLE)
        LOG.close()
    return 0 if summary.get("passed") else 1

START = time.time()
if __name__ == "__main__":
    sys.exit(main())
