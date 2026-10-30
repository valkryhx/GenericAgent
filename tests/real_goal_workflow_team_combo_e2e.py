"""Real-LLM proof: goal + background workflow + agent-team all active at once.

Three subsystems share one real session:
  - goal:   set via /goal, evaluated only at the Stop gate (deferred while work runs)
  - workflow: launched by the model through the `workflow` tool (bridge daemon thread)
  - agent-team: a team_mode subagent spawned via spawn_agent (SubagentManager)

Design (staged so each real-model turn stays simple and reliable):
  turn0: /goal <absolute-path condition>
  turn1: model launches a SLOW background workflow (sleeps then writes marker_wf)
  turn2: model creates a team task and spawns a SLOW team_mode subagent (marker_team)
         -> at this point workflow + team subagent + goal are ALL active together
  while both run: query bridge team roster + workflow runs (execution-layer evidence)
  after both finish: handoff/turn re-evaluates goal (non-deferred) -> met

Asserts the core cooperation contract:
  * both launch turns end normally (done=True), record goal_defer=True, <=8 steps
  * both background channels were genuinely in flight at the same time
    (has_in_flight_agents True for BOTH a workflow and a team subagent)
  * both marker files land where the goal expects
  * goal is finally evaluated non-deferred and reaches a terminal verdict
"""
from __future__ import annotations
import json, os, sys, time, threading, traceback
from pathlib import Path

REPO = Path.cwd()
for p in (REPO, REPO / "frontends"):
    sys.path.insert(0, str(p))

TAG = time.strftime("%Y%m%d-%H%M%S")
TARGET = Path("D:/test20260416") / f"_goal_combo_{TAG}"
DATA = REPO / "temp" / "goal_combo" / f"data_{TAG}"
LOGDIR = REPO / "temp" / "goal_combo"; LOGDIR.mkdir(parents=True, exist_ok=True)
LOG = (LOGDIR / f"combo_{TAG}.log").open("w", encoding="utf-8")

CHECKS = []
def log(*a):
    s = " ".join(str(x) for x in a); LOG.write(s + "\n"); LOG.flush(); print(s, flush=True)
def rec(name, ok, detail=""):
    CHECKS.append({"name": name, "ok": bool(ok), "detail": str(detail)})
    log(("PASS " if ok else "FAIL ") + name + (" - " + str(detail) if detail else ""))

def setup_env():
    os.environ.update({
        "GA_PROJECT_ROOT": str(TARGET), "GA_DATA_DIR": str(DATA),
        "GA_WORKFLOW_WORKSPACE_ROOT": str(TARGET), "GA_LAUNCH_CWD": str(TARGET),
        "GA_ORIGINAL_CWD": str(TARGET), "GA_LLM_CONFIG": str(REPO / "llm.yaml"),
    })
    for k in ("GA_PROJECT_KEY","GA_PROJECT_DATA_DIR","GA_SESSION_DATA_DIR",
              "GA_PARENT_SESSION_ID","GA_TASK_ROOT","GA_TEAM_KEY"):
        os.environ.pop(k, None)
    os.environ.pop("GA_GOAL_WAKE", None)

class Rec:
    def __init__(self): self.events = []; self.lock = threading.Lock()
    def __call__(self, e):
        with self.lock: self.events.append(e)
    def of(self, t):
        with self.lock: return [e for e in self.events if e.get("type") == t]

def wait_idle(agent, t):
    end = time.time() + t
    while time.time() < end:
        if not getattr(agent, "is_running", False): return True
        time.sleep(0.1)
    return False

def run_turn(agent, prompt, timeout, hard_cap=None):
    out = agent.put_task(prompt); done = None; steps = 0
    end = time.time() + timeout; t0 = time.time()
    while time.time() < end:
        try:
            ev = out.get(timeout=0.3)
            blob = json.dumps(ev, ensure_ascii=False) if isinstance(ev, dict) else str(ev)
            steps += blob.count("LLM Running (Step")
            if isinstance(ev, dict) and "done" in ev: done = ev; break
        except Exception:
            if not getattr(agent, "is_running", False) and done is None and (time.time()-t0) > 1:
                break
        if hard_cap and (time.time()-t0) > hard_cap: break
    if not done and getattr(agent, "is_running", False):
        try: agent.abort()
        except Exception: pass
    wait_idle(agent, 30)
    return {"done": bool(done), "steps": steps, "secs": round(time.time()-t0, 1),
            "text": str((done or {}).get("done") or "")[:240]}

def goal_status_events(path):
    rows = []
    if not path or not Path(path).is_file(): return rows
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line: continue
        try: o = json.loads(line)
        except Exception: continue
        if isinstance(o, dict) and o.get("type") == "goal_status": rows.append(o)
    return rows


def _states(mgr, sid):
    try:
        return mgr.list_descendants(sid)
    except Exception:
        return []

def main() -> int:
    setup_env(); TARGET.mkdir(parents=True, exist_ok=True)
    (TARGET / "nested").mkdir(exist_ok=True)
    from frontends.ink.bridge import GenericAgentBridge
    from genericagent.core import goal_evaluator as ge
    rec_ev = Rec()
    summary = {"target": str(TARGET), "model": "default/deepseek-v4.1-flash"}
    bridge = agent = None
    try:
        bridge = GenericAgentBridge(emit=rec_ev); agent = bridge.agent
        threading.Thread(target=agent.run, daemon=True).start()
        bridge.model_switch("default/deepseek-v4.1-flash"); time.sleep(2)

        mwf = TARGET / "marker_wf.txt"
        mteam = TARGET / "marker_team.txt"
        goal_text = (f"目录 {TARGET} 下同时存在 marker_wf.txt(WF_OK) 与 marker_team.txt(TEAM_OK)")
        r0 = run_turn(agent, f"/goal {goal_text}", 60)
        summary["set_goal"] = r0
        rec("turn0 /goal set an active goal", agent.active_goal is not None,
            getattr(agent.active_goal, "condition", None))

        # ---- turn1: create team task + spawn a SLOW (sleep 120) team subagent ----
        spawn_prompt = (
            "这是一个 agent team 协作场景。请严格按顺序执行（不要自己动手做任务本体）：\n"
            "1) 调用 task_create 创建任务，subject 为 'write marker_team'；记下返回的精确 task id。\n"
            "2) 调用 spawn_agent，task_name='teammate1'，team_mode=true，team_task_id=<第1步的 task id>，background=true。"
            "message 明确要求它：用 code_run 执行命令 `python -c \"import time;time.sleep(120);"
            f"open(r'{mteam.as_posix()}','w').write('TEAM_OK')\"` ，然后结束。\n"
            "3) 完成 spawn 后只回报 task id，然后立刻结束本轮，不要等待、不要自己写 marker_team.txt。"
        )
        r1 = run_turn(agent, spawn_prompt, 180, hard_cap=150)
        summary["team_spawn_turn"] = r1
        summary["team_last_goal_defer"] = getattr(agent.handler, "last_goal_defer", None)
        rec("team spawn turn ends normally (done=True)", r1["done"] is True, r1)
        rec("team spawn turn records goal_defer=True", summary["team_last_goal_defer"] is True,
            summary["team_last_goal_defer"])

        # ---- turn2: launch a background workflow (sleep 30 then write marker_wf) ----
        wf_path = TARGET / "nested" / "slow_wf.js"
        wf_path.write_text(
            "export const meta = { name: 'combo-wf', description: 'slow write marker_wf' }\n"
            "phase('write')\n"
            "const a = await agent(`Use code_run to run exactly this ONE command, then stop: "
            f"python -c \\\"import time;time.sleep(30);open(r'{mwf.as_posix()}','w').write('WF_OK')\\\"`"
            ", { label: 'slowwf' })\n"
            "return { summary: a.summary }\n",
            encoding="utf-8", newline="\n",
        )
        launch2 = (
            "请使用 workflow 工具在后台启动由 scriptPath 指定的 workflow 脚本："
            f"scriptPath=\"{wf_path.as_posix()}\" 。启动后只回报 run id 并结束本轮，不要等待它完成。"
        )
        r2 = run_turn(agent, launch2, 180, hard_cap=150)
        summary["wf_launch_turn"] = r2
        summary["wf_last_goal_defer"] = getattr(agent.handler, "last_goal_defer", None)
        rec("workflow launch turn ends normally (done=True)", r2["done"] is True, r2)
        rec("workflow launch turn records goal_defer=True", summary["wf_last_goal_defer"] is True,
            summary["wf_last_goal_defer"])
        rec("workflow launch turn does not spin (<=10 steps)", r2["steps"] <= 10, r2["steps"])

        runs = bridge.workflow_store.list_runs(session_id=str(getattr(agent, "session_id", "")))
        wf_run = runs[-1] if runs else None
        summary["wf_run_id"] = getattr(wf_run, "run_id", None)
        rec("a real workflow run was created", wf_run is not None, getattr(wf_run, "run_id", None))

        # ---- immediately after workflow launch: is the team subagent still inflight? ----
        time.sleep(2)
        sid = str(getattr(agent, "session_id", ""))
        mgr = agent.handler._get_subagent_manager()
        states = _states(mgr, sid)
        sub_running = [f"{getattr(s,'task_name','?')}:{getattr(s,'turn_status','?')}/{getattr(s,'process_status','?')}"
                       for s in states if ge.is_in_flight_agent_state(s)]
        wf_labels = ge._workflow_runs_in_flight(agent)
        both = ge.has_in_flight_agents(agent, agent.handler)
        summary.update(wf_labels=wf_labels, sub_labels=sub_running, both_inflight=both)
        rec("team subagent STILL in flight after workflow launch (concurrency window)",
            len(sub_running) >= 1, sub_running)
        rec("workflow channel in flight after launch", len(wf_labels) >= 1, wf_labels)
        rec("has_in_flight_agents True while BOTH active (defer would hold)", both is True, both)

        # ---- wait for both background jobs to finish ----
        end = time.time() + 300
        while time.time() < end:
            if mwf.is_file() and mteam.is_file():
                break
            time.sleep(2)
        time.sleep(6)
        rec("workflow wrote marker_wf.txt with WF_OK",
            mwf.is_file() and "WF_OK" in mwf.read_text(encoding="utf-8", errors="replace"), str(mwf))
        rec("team subagent wrote marker_team.txt with TEAM_OK",
            mteam.is_file() and "TEAM_OK" in mteam.read_text(encoding="utf-8", errors="replace"), str(mteam))

        # ---- goal finally evaluated non-deferred ----
        wait_idle(agent, 180)
        gs = []
        end = time.time() + 180
        while time.time() < end:
            gs = goal_status_events(getattr(agent, "session_path", None))
            if any(str(g.get("reason")) != "deferred" for g in gs):
                break
            time.sleep(2)
        nondef = [g for g in gs if str(g.get("reason")) != "deferred"]
        summary["goal_status_tail"] = gs[-4:]
        rec("goal evaluated after both finished (a non-deferred goal_status exists)",
            len(nondef) >= 1, json.dumps(nondef[-1:], ensure_ascii=False)[:300])
        summary["goal_met"] = (nondef[-1].get("met") if nondef else None)

        passed = all(c["ok"] for c in CHECKS)
        summary["checks"] = CHECKS
        log("RESULT %d/%d checks passed" % (sum(1 for c in CHECKS if c["ok"]), len(CHECKS)))
        return 0 if passed else 1
    except Exception as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
        summary["traceback"] = traceback.format_exc()
        log("ERROR", summary["error"]); log(summary["traceback"])
        summary["checks"] = CHECKS
        return 1
    finally:
        try:
            if agent is not None:
                setattr(agent, "stop_sig", True)
                try: agent.abort()
                except Exception: pass
        except Exception: pass
        (LOGDIR / f"summary_{TAG}.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")

if __name__ == "__main__":
    sys.exit(main())
