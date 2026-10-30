"""PROBE (pre-fix): how does an active goal interact with a background workflow?

Reproduces the suspected collision: goal active -> main launches a background
workflow -> the goal Stop gate evaluates the (still-empty) transcript, blocks,
and drives the main agent to keep looping instead of yielding to the workflow.

This is a diagnostic, not a pass/fail gate. It records:
  - last_goal_defer during the launch turn (expected: False = workflow invisible)
  - how many model steps ran in the single launch turn (goal-block re-loop count)
  - whether the workflow completed and produced the marker file
  - final goal state
"""
from __future__ import annotations
import json, os, sys, time, threading, traceback
from pathlib import Path

REPO = Path.cwd()
for p in (REPO, REPO / "frontends"):
    sys.path.insert(0, str(p))
RUN_TAG = time.strftime("%Y%m%d-%H%M%S")
TARGET = Path("D:/test20260416") / f"_goal_wf_probe_{RUN_TAG}"
DATA = REPO / "temp" / "goal_wf" / f"data_{RUN_TAG}"
LOGDIR = REPO / "temp" / "goal_wf"; LOGDIR.mkdir(parents=True, exist_ok=True)
LOG = (LOGDIR / f"probe_{RUN_TAG}.log").open("w", encoding="utf-8")
def log(*a):
    line=" ".join(str(x) for x in a); LOG.write(line+"\n"); LOG.flush(); print(line, flush=True)

def setup_env():
    os.environ.update({
        "GA_PROJECT_ROOT": str(TARGET), "GA_DATA_DIR": str(DATA),
        "GA_WORKFLOW_WORKSPACE_ROOT": str(TARGET), "GA_LAUNCH_CWD": str(TARGET),
        "GA_ORIGINAL_CWD": str(TARGET), "GA_LLM_CONFIG": str(REPO/"llm.yaml"),
    })
    os.environ.pop("GA_GOAL_WAKE", None)
    for k in ("GA_PROJECT_KEY","GA_PROJECT_DATA_DIR","GA_SESSION_DATA_DIR",
              "GA_PARENT_SESSION_ID","GA_TASK_ROOT","GA_TEAM_KEY"):
        os.environ.pop(k, None)

class Rec:
    def __init__(self): self.events=[]; self.lock=threading.Lock()
    def __call__(self,e):
        with self.lock: self.events.append(e)
    def of(self,t):
        with self.lock: return [e for e in self.events if e.get("type")==t]

def wait_idle(agent, t):
    end=time.time()+t
    while time.time()<end:
        if not getattr(agent,"is_running",False): return True
        time.sleep(0.1)
    return False

def run_turn(agent, prompt, timeout, hard_cap=None):
    out=agent.put_task(prompt); done=None; steps=0; end=time.time()+timeout
    t0=time.time()
    while time.time()<end:
        try:
            ev=out.get(timeout=0.3)
            blob=json.dumps(ev, ensure_ascii=False) if isinstance(ev,dict) else str(ev)
            steps += blob.count("LLM Running (Step")
            if isinstance(ev,dict) and "done" in ev: done=ev; break
        except Exception:
            if not getattr(agent,"is_running",False) and done is None and (time.time()-t0)>1:
                break
        if hard_cap and (time.time()-t0)>hard_cap: break
    if not done and getattr(agent,"is_running",False):
        try: agent.abort()
        except Exception: pass
    wait_idle(agent, 30)
    return {"done":bool(done), "steps":steps, "secs":round(time.time()-t0,1),
            "text":str((done or {}).get("done") or "")[:300]}

def main():
    setup_env(); TARGET.mkdir(parents=True, exist_ok=True)
    from frontends.ink.bridge import GenericAgentBridge
    rec=Rec(); summary={"target":str(TARGET),"model":"default/deepseek-v4.1-flash"}
    bridge=agent=None
    try:
        bridge=GenericAgentBridge(emit=rec); agent=bridge.agent
        threading.Thread(target=agent.run, daemon=True).start()
        bridge.model_switch("default/deepseek-v4.1-flash"); time.sleep(2)

        marker_name="wf_out.txt"
        goal_text=f"目录 {TARGET} 下存在文件 {marker_name} 且内容包含 WF_OK"
        r0=run_turn(agent, f"/goal {goal_text}", 60)
        summary["set_goal"]=r0
        summary["goal_active_after_set"]=agent.active_goal is not None

        wf_script=(
            "export const meta = { name: 'probe-wf', description: 'write wf_out.txt' }\n"
            "phase('write')\n"
            f"const a = await agent(`IN CURRENT WORKSPACE write a file named {marker_name} "
            f"whose content is exactly WF_OK using file_write. Do not write elsewhere.`, {{ label: 'writer' }})\n"
            "return { summary: a.summary }\n"
        )
        launch_prompt=(
            "请使用 workflow 工具在后台启动下面这段 workflow 脚本（script 参数），"
            "启动后只回报 run id 并结束本轮，不要等待它完成、不要调用 wait_agent：\n\n"
            + wf_script
        )
        r1=run_turn(agent, launch_prompt, 150, hard_cap=120)
        summary["launch_turn"]=r1
        summary["last_goal_defer"]=getattr(agent.handler,"last_goal_defer",None)
        summary["goal_iterations_after_launch"]=getattr(agent.active_goal,"iterations",None)
        summary["last_turn_outcome"]=getattr(agent,"last_turn_outcome",None)

        # let the workflow finish in background
        wf=None; end=time.time()+240; rid=None
        runs = bridge.workflow_store.list_runs(session_id=str(getattr(agent,"session_id","")))
        summary["runs_seen"]=[getattr(r,"run_id",None) for r in runs]
        while time.time()<end:
            runs = bridge.workflow_store.list_runs(session_id=str(getattr(agent,"session_id","")))
            if runs:
                run=runs[-1]; rid=run.run_id
                if run.status in {"succeeded","degraded","failed","cancelled","killed","interrupted"}:
                    wf={"runId":rid,"status":run.status,"ws":(run.metadata or {}).get("workspacePath")}
                    break
            time.sleep(2)
        summary["workflow"]=wf
        mk=None
        for cand in [TARGET/marker_name]:
            if cand.is_file(): mk=str(cand)
        summary["marker"]=mk
        summary["goal_active_end"]=agent.active_goal is not None
        log("SUMMARY", json.dumps(summary, ensure_ascii=False, indent=2))
    except Exception as exc:
        summary["error"]=f"{type(exc).__name__}: {exc}"; summary["traceback"]=traceback.format_exc()
        log("ERROR", summary["error"]); log(summary["traceback"])
    finally:
        try:
            if agent is not None:
                setattr(agent,"stop_sig",True)
                try: agent.abort()
                except Exception: pass
        except Exception: pass
    (LOGDIR/f"probe_summary_{RUN_TAG}.json").write_text(json.dumps(summary,ensure_ascii=False,indent=2),encoding="utf-8",newline="\n")

if __name__=="__main__":
    main()
