"""Controlled real-LLM proof of Stage 2-A at the actual Stop gate.

- real GenericAgent + real LLM (default/deepseek-v4.1-flash)
- real background sub-agent spawned (slow, sleeps) so it is genuinely in flight
- call handler.run_stop_gate() exactly as the loop does at turn end:
    * while child in flight -> defer: action must be 'none', no model eval,
      transcript records goal_status(reason='deferred')
    * after child finished    -> evaluate: model eval runs, no evaluator_error
"""
from __future__ import annotations
import json, os, sys, time, threading, traceback
from pathlib import Path
REPO = Path.cwd()
for p in (REPO, REPO / "frontends"):
    sys.path.insert(0, str(p))
TAG = time.strftime("%Y%m%d-%H%M%S")
TARGET = Path("D:/test20260416") / f"_gate2a_{TAG}"
DATA = REPO / "temp" / "gate2a" / f"data_{TAG}"
LOGDIR = REPO / "temp" / "gate2a"; LOGDIR.mkdir(parents=True, exist_ok=True)
lg = (LOGDIR / f"gate_{TAG}.log").open("w", encoding="utf-8")
def log(*a):
    s=" ".join(str(x) for x in a); lg.write(s+"\n"); lg.flush(); print(s,flush=True)
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
from genericagent.core.handler import GenericAgentHandler
from genericagent.core import goal_evaluator as ge
from genericagent import paths as ga_paths

def goal_status_events(path):
    out=[]
    if not path or not Path(path).is_file(): return out
    for ln in Path(path).read_text(encoding="utf-8",errors="replace").splitlines():
        ln=ln.strip()
        if not ln: continue
        try:o=json.loads(ln)
        except Exception:continue
        if isinstance(o,dict) and o.get("type")=="goal_status": out.append(o)
    return out

rec_events=[]; bridge=None; agent=None
summary={"target":str(TARGET),"dataRoot":str(DATA),"model":"default/deepseek-v4.1-flash"}
try:
    bridge=GenericAgentBridge(emit=lambda e:rec_events.append(e)); agent=bridge.agent
    th=threading.Thread(target=agent.run,daemon=True); th.start()
    bridge.model_switch("default/deepseek-v4.1-flash"); time.sleep(2)

    # register goal hook for real
    agent._set_goal("The background child task must have completed successfully.")
    log("active_goal:", getattr(agent,"active_goal",None))

    project_root=str(getattr(agent,"project_root",None) or ga_paths.PROJECT_ROOT)
    tool_cwd=str(getattr(agent,"tool_cwd",None) or project_root)
    h=GenericAgentHandler(agent, agent.history, tool_cwd, workspace_root=project_root)
    h.hook_runtime=getattr(agent,"hook_runtime",None)
    if h.hook_runtime is None:
        from genericagent.hooks.runtime import hook_runtime_for_agent
        h.hook_runtime=hook_runtime_for_agent(agent)

    # instrument prompt_runner to count real model evaluator invocations
    runner_calls=[]
    orig=h.hook_runtime.prompt_runner
    def wrapped(kind,prompt,payload):
        runner_calls.append(kind)
        return orig(kind,prompt,payload) if orig else "{}"
    h.hook_runtime.prompt_runner=wrapped

    mgr=h._get_subagent_manager()
    marker=TARGET/"child_done.txt"
    msg=(f"Work slowly. First use code_run to run: python -c \"import time;time.sleep(45);"
         f"open(r'{marker}','w').write('OK')\". Then reply done.")
    res=mgr.spawn_agent("slowchild",msg,parent_session_id=str(agent.session_id or ""),background=True)
    log("spawned pid:", getattr(res,"pid",None))
    time.sleep(6)
    inflight=ge.has_in_flight_agents(agent,h)
    rec("child is genuinely in flight before turn end",inflight,inflight)
    summary["in_flight_before_gate"]=inflight

    before=len(runner_calls)
    gs_before=len(goal_status_events(getattr(agent,"session_path",None)))
    action,text,agg=h.run_stop_gate({})
    summary["defer_action"]=action
    summary["defer_text"]=text[:200]
    rec("Stop gate while child in flight returns 'none' (turn NOT blocked)",action=="none",action)
    rec("goal evaluator model was NOT called during defer",len(runner_calls)==before,
        f"calls_before={before} calls_after={len(runner_calls)}")
    gs=goal_status_events(getattr(agent,"session_path",None))
    deferred=[g for g in gs if str(g.get("reason"))=="deferred"]
    rec("transcript recorded goal_status(reason='deferred')",
        len(deferred)>=1, json.dumps(deferred[-1:],ensure_ascii=False)[:260])
    rec("goal still active after deferred gate (not cleared/failed)",
        getattr(agent,"active_goal",None) is not None,
        getattr(getattr(agent,"active_goal",None),"iterations",None))

    # wait for child to finish
    end=time.time()+180
    while time.time()<end and not marker.is_file(): time.sleep(2)
    time.sleep(10)  # let process status settle
    done_inflight=ge.has_in_flight_agents(agent,h)
    rec("child no longer in flight after it finished",not done_inflight, done_inflight)
    summary["in_flight_after_done"]=done_inflight

    # now the gate must actually evaluate (model called), no evaluator_error
    before2=len(runner_calls)
    action2,text2,agg2=h.run_stop_gate({})
    summary["evaluate_action"]=action2
    summary["evaluate_text"]=(text2 or "")[:300]
    rec("Stop gate after child done actually invoked goal evaluator (model called)",
        len(runner_calls)>before2, f"calls {before2}->{len(runner_calls)}")
    gs2=goal_status_events(getattr(agent,"session_path",None))
    errs=[g for g in gs2 if str(g.get("reason"))=="evaluator_error"]
    last=gs2[-1] if gs2 else {}
    rec("evaluator did NOT error on the real run",len(errs)==0,
        json.dumps(errs[-1:],ensure_ascii=False)[:200])
    rec("evaluator produced a parsed verdict (last goal_status not error)",
        str(last.get("reason"))!="evaluator_error", json.dumps(last,ensure_ascii=False)[:260])
    summary["final_goal_status"]=gs2[-3:]
except Exception as exc:
    summary["error"]=f"{type(exc).__name__}: {exc}"; summary["traceback"]=traceback.format_exc()
    log("ERROR",summary["error"]); log(summary["traceback"])
finally:
    try:
        if agent is not None:
            agent.stop_sig=True
            try: agent.abort()
            except Exception: pass
    except Exception: pass

out=LOGDIR/f"summary_{TAG}.json"
out.write_text(json.dumps({"summary":summary,"checks":CHECKS},ensure_ascii=False,indent=2),encoding="utf-8",newline="\n")
npass=sum(1 for c in CHECKS if c["ok"])
log(f"RESULT {npass}/{len(CHECKS)} -> {out}")
sys.exit(0 if CHECKS and npass==len(CHECKS) else 1)
