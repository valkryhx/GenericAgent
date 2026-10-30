"""Stage F phase 4 proof: propose_goal end-to-end.

Part A (deterministic, real GenericAgent + real handler + real bridge runtime):
  - handler.do_propose_goal(ask_user=false) -> queues /goal, origin=proposal_direct
  - a worker thread approves the approval request -> ask_user path sets goal
  - gate: subagent / disabled / plan-mode rejected
Part B (real LLM): ask the model to call propose_goal, approve it in a worker
  thread, then confirm active_goal becomes set with origin proposal_approved.
"""
from __future__ import annotations
import json, os, sys, time, threading, traceback
from pathlib import Path
REPO = Path.cwd()
for p in (REPO, REPO / "frontends"):
    sys.path.insert(0, str(p))
TAG = time.strftime("%Y%m%d-%H%M%S")
TARGET = Path("D:/test20260416") / f"_p4_{TAG}"
DATA = REPO / "temp" / "p4" / f"data_{TAG}"
LOGDIR = REPO / "temp" / "p4"; LOGDIR.mkdir(parents=True, exist_ok=True)
lg = (LOGDIR / f"p4_{TAG}.log").open("w", encoding="utf-8")
def log(*a):
    s=" ".join(str(x) for x in a); lg.write(s+"\n"); lg.flush(); print(s, flush=True)
CHECKS=[]
def rec(n,ok,d=""):
    CHECKS.append({"name":n,"ok":bool(ok),"detail":str(d)})
    log(("PASS " if ok else "FAIL ")+n+(" - "+str(d) if d else ""))
os.environ.update({"GA_PROJECT_ROOT":str(TARGET),"GA_DATA_DIR":str(DATA),
  "GA_WORKFLOW_WORKSPACE_ROOT":str(TARGET),"GA_LAUNCH_CWD":str(TARGET),
  "GA_ORIGINAL_CWD":str(TARGET),"GA_LLM_CONFIG":str(REPO/"llm.yaml")})
for k in ("GA_PROJECT_KEY","GA_PROJECT_DATA_DIR","GA_SESSION_DATA_DIR","GA_PARENT_SESSION_ID","GA_TASK_ROOT","GA_TEAM_KEY"):
    os.environ.pop(k,None)
TARGET.mkdir(parents=True, exist_ok=True)

from frontends.ink.bridge import GenericAgentBridge
from genericagent.core import goal_proposal as gp
from genericagent.core import goal as goal_core

events=[]
bridge=GenericAgentBridge(emit=lambda e: events.append(e))
agent=bridge.agent
try:
    bridge.model_switch("default/deepseek-v4.1-flash"); time.sleep(2)

    # ---- Part A1: direct path via real handler ----
    from genericagent.core.handler import GenericAgentHandler
    from genericagent import paths as ga_paths
    project_root=str(getattr(agent,"project_root",None) or ga_paths.PROJECT_ROOT)
    tool_cwd=str(getattr(agent,"tool_cwd",None) or project_root)
    h=GenericAgentHandler(agent, agent.history, tool_cwd, workspace_root=project_root)
    h.hook_runtime=getattr(agent,"hook_runtime",None)
    out=h.do_propose_goal({"condition":"unit tests are all green","ask_user":False}, None)
    rec("A1 direct propose_goal returns ok", out.data.get("status")=="ok", out.data)
    time.sleep(1.0)  # let queued /goal be consumed by the agent run loop
    g=getattr(agent,"active_goal",None)
    rec("A1 /goal took effect after direct proposal", g is not None and g.condition=="unit tests are all green",
        getattr(g,"condition",None))
    rec("A1 origin recorded as proposal_direct", getattr(g,"origin",None)==goal_core.ORIGIN_PROPOSAL_DIRECT, getattr(g,"origin",None))
    # clear it so Part B starts clean
    agent._clear_goal(met=False, reason="reset for test")

    # ---- Part A2: gate rejections ----
    agent.is_subagent=True
    o2=h.do_propose_goal({"condition":"x"},None); agent.is_subagent=False
    rec("A2 subagent proposal rejected", o2.data.get("reason")=="goal_proposal_rejected", o2.data.get("msg"))
    with_disable = {"GA_MODEL_PROPOSED_GOALS":"disabled"}
    saved={k:os.environ.get(k) for k in with_disable}
    os.environ.update(with_disable)
    o3=h.do_propose_goal({"condition":"y"},None)
    for k,v in saved.items():
        if v is None: os.environ.pop(k,None)
        else: os.environ[k]=v
    rec("A2 disabled setting rejected", o3.data.get("reason")=="goal_proposal_rejected", o3.data.get("msg"))

    # ---- Part B: real LLM proposes, worker approves ----
    approvals=[]
    stop_flag=threading.Event()
    def approver():
        while not stop_flag.is_set():
            reqs=[e for e in events if e.get("type")=="goal_proposal_request"]
            # approve the newest unapproved request
            for r in reqs:
                rid=r.get("requestId")
                if rid and rid not in approvals:
                    time.sleep(0.5)
                    bridge.goal_proposal_response(rid, "approve")
                    approvals.append(rid)
                    log("approved proposal:", r.get("condition"))
            time.sleep(0.2)
    th=threading.Thread(target=approver,daemon=True); th.start()

    # Drive a real turn asking the model to propose a goal for a small task.
    prompt=("Use the propose_goal tool to propose this session goal: 'a file goal_demo.txt exists in the "
            "workspace root whose content is DONE'. Pass ask_user true (just propose it; do not create the "
            "file yet). After the tool returns, stop and report what happened.")
    q=agent.put_task(prompt, source="user")
    end=time.time()+180; done=None
    while time.time()<end:
        try:
            ev=q.get(timeout=0.3)
            if isinstance(ev,dict) and "done" in ev: done=ev; break
        except Exception:
            if not getattr(agent,"is_running",False) and done is not None: break
    stop_flag.set(); th.join(timeout=3)
    rec("B real LLM emitted at least one goal_proposal_request", len([e for e in events if e.get("type")=="goal_proposal_request"])>=1,
        len([e for e in events if e.get("type")=="goal_proposal_request"]))
    rec("B we approved a proposal", len(approvals)>=1, approvals)
    time.sleep(1.5)
    g2=getattr(agent,"active_goal",None)
    rec("B active goal set from approved proposal", g2 is not None and "goal_demo.txt" in (getattr(g2,"condition","") or ""),
        getattr(g2,"condition",None))
    rec("B origin is proposal_approved", getattr(g2,"origin",None)==goal_core.ORIGIN_PROPOSAL_APPROVED, getattr(g2,"origin",None))
except Exception as exc:
    log("ERROR", f"{type(exc).__name__}: {exc}"); log(traceback.format_exc())
    rec("no unexpected exception", False, f"{type(exc).__name__}: {exc}")
finally:
    try:
        agent.stop_sig=True; agent.abort()
    except Exception: pass

out=LOGDIR/f"summary_{TAG}.json"
out.write_text(json.dumps({"checks":CHECKS},ensure_ascii=False,indent=2),encoding="utf-8",newline="\n")
npass=sum(1 for c in CHECKS if c["ok"])
log(f"RESULT {npass}/{len(CHECKS)} -> {out}")
sys.exit(0 if CHECKS and npass==len(CHECKS) else 1)
