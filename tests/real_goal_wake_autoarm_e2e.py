"""Real-LLM proof: run() finally AUTO-arms a retry when a turn ends max_turns.

Unlike real_goal_wake_e2e (which arms manually), here the turn itself ends
early: we wrap the REAL agent_runner_loop with max_turns=1 so the goal turn
returns MAX_TURNS_EXCEEDED. run()'s finally must call
_maybe_arm_goal_wake_after_turn -> scheduler.arm_retry, and the scheduler must
then deliver a real new turn through the Ink bridge.
"""
from __future__ import annotations
import json, os, sys, time, threading, traceback
from pathlib import Path

REPO = Path.cwd()
for p in (REPO, REPO / "frontends"):
    sys.path.insert(0, str(p))

RUN_TAG = time.strftime("%Y%m%d-%H%M%S")
TARGET = Path("D:/test20260416") / f"_goal_wake_auto_{RUN_TAG}"
DATA = REPO / "temp" / "goal_wake" / f"data_auto_{RUN_TAG}"
LOGDIR = REPO / "temp" / "goal_wake"; LOGDIR.mkdir(parents=True, exist_ok=True)
LOG = (LOGDIR / f"driver_auto_{RUN_TAG}.log").open("w", encoding="utf-8")
def log(*a):
    line=" ".join(str(x) for x in a); LOG.write(line+"\n"); LOG.flush(); print(line, flush=True)
CHECKS=[]
def rec(name, ok, detail=""):
    CHECKS.append({"name":name,"ok":bool(ok),"detail":str(detail)})
    log(("PASS " if ok else "FAIL ")+name+(" - "+str(detail) if detail else ""))

def setup_env():
    os.environ.update({
        "GA_PROJECT_ROOT": str(TARGET), "GA_DATA_DIR": str(DATA),
        "GA_WORKFLOW_WORKSPACE_ROOT": str(TARGET), "GA_LAUNCH_CWD": str(TARGET),
        "GA_ORIGINAL_CWD": str(TARGET), "GA_LLM_CONFIG": str(REPO/"llm.yaml"),
        "GA_GOAL_WAKE": "1", "GA_GOAL_RETRY_MAX": "5",
    })
    for k in ("GA_PROJECT_KEY","GA_PROJECT_DATA_DIR","GA_SESSION_DATA_DIR",
              "GA_PARENT_SESSION_ID","GA_TASK_ROOT","GA_TEAM_KEY"):
        os.environ.pop(k, None)

def goal_events(root):
    rows=[]; p=Path(root)/"logs"/"goal_events.jsonl"
    if p.is_file():
        for line in p.read_text(encoding="utf-8",errors="replace").splitlines():
            line=line.strip()
            if line:
                try: rows.append(json.loads(line))
                except Exception: pass
    return rows

class Rec:
    def __init__(self): self.events=[]; self.lock=threading.Lock()
    def __call__(self,e):
        with self.lock: self.events.append(e)
    def running_ids(self):
        with self.lock:
            return sorted({e.get("taskId") for e in self.events
                           if e.get("type")=="status" and e.get("status")=="running" and e.get("taskId") is not None})

def wait_idle(agent, t):
    end=time.time()+t
    while time.time()<end:
        if not getattr(agent,"is_running",False): return True
        time.sleep(0.1)
    return False

def run_turn(agent, prompt, timeout):
    out=agent.put_task(prompt); done=None
    end=time.time()+timeout
    while time.time()<end:
        try:
            ev=out.get(timeout=0.3)
            if isinstance(ev,dict) and "done" in ev: done=ev; break
        except Exception:
            if not getattr(agent,"is_running",False): break
    wait_idle(agent, 20)
    return bool(done)

def main():
    setup_env(); TARGET.mkdir(parents=True, exist_ok=True)
    from frontends.ink.bridge import GenericAgentBridge
    import genericagent.core.app as appmod
    recorder=Rec(); summary={"target":str(TARGET),"dataRoot":str(DATA),"model":"default/deepseek-v4.1-flash"}
    bridge=agent=None
    try:
        bridge=GenericAgentBridge(emit=recorder); agent=bridge.agent
        threading.Thread(target=agent.run, daemon=True).start()
        bridge.model_switch("default/deepseek-v4.1-flash"); time.sleep(2)
        # set goal first (normal turn, not capped)
        run_turn(agent, "/goal 主目录下必须存在文件 auto_done.txt 且内容包含 AUTO_OK", 60)
        rec("goal set before capping", getattr(agent,"active_goal",None) is not None)
        # now cap max_turns to 1 so the next real turn ends MAX_TURNS_EXCEEDED
        real_loop=appmod.agent_runner_loop
        def capped(client, system_prompt, user_input, handler, tools_schema, max_turns=1, verbose=True, initial_user_content=None):
            return real_loop(client, system_prompt, user_input, handler, tools_schema, max_turns=1, verbose=verbose, initial_user_content=initial_user_content)
        appmod.agent_runner_loop=capped
        before_events=len(goal_events(DATA)); before_ids=set(recorder.running_ids())
        run_turn(agent, "好的，开始推进目标。先建文件再写入内容。", 90)
        # now expect an auto-armed retry: goal_interruption armed+fired beyond before_events
        deadline=time.time()+90; fired=False; newids=[]
        while time.time()<deadline:
            evs=goal_events(DATA)[before_events:]
            if any(e.get("event")=="goal_interruption" and e.get("outcome")=="fired" for e in evs):
                fired=True
            newids=sorted(set(recorder.running_ids())-before_ids)
            if fired and newids: break
            time.sleep(0.5)
        rec("run() finally AUTO-armed and fired a retry", fired)
        rec("auto-retry produced a real new wake turn", bool(newids), str(newids))
        # stop the retry chain to keep it bounded
        os.environ["GA_GOAL_WAKE"]="0"
        try: agent._cancel_goal_wake("test_done")
        except Exception: pass
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
    out=LOGDIR/f"summary_auto_{RUN_TAG}.json"
    out.write_text(json.dumps({"summary":summary,"checks":CHECKS},ensure_ascii=False,indent=2),encoding="utf-8",newline="\n")
    log("SUMMARY ->",str(out))
    n=sum(1 for c in CHECKS if c["ok"]); log(f"RESULT {n}/{len(CHECKS)} checks passed")
    return 0 if CHECKS and n==len(CHECKS) else 1

if __name__=="__main__":
    sys.exit(main())
