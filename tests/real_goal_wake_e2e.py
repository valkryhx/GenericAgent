"""Real-LLM proof for goal auto-wake (phases 2-B / 3).

Exercises the REAL chain: GoalWakeScheduler timer thread -> Ink bridge
``_handle_goal_wake`` -> ``agent.put_task(source='goal_wake')`` -> ``agent.run``
-> real model produces a new turn. Delay constants are shrunk via a test shim
so the run is quick; everything else (scheduler, bridge, agent, model) is real.

Phases:
  A default-off safety : goal set, turn ends, NO wake scheduled.
  B retry wake         : GA_GOAL_WAKE=1, arm a retry; a new real turn runs.
  C checkin wake       : arm a check-in; a new real turn runs.
Model: default profile -> deepseek-v4.1-flash.
"""
from __future__ import annotations
import json, os, sys, time, threading, traceback
from pathlib import Path

REPO = Path.cwd()
for p in (REPO, REPO / "frontends"):
    sys.path.insert(0, str(p))

RUN_TAG = time.strftime("%Y%m%d-%H%M%S")
TARGET = Path("D:/test20260416") / f"_goal_wake_{RUN_TAG}"
DATA = REPO / "temp" / "goal_wake" / f"data_{RUN_TAG}"
LOGDIR = REPO / "temp" / "goal_wake"
LOGDIR.mkdir(parents=True, exist_ok=True)
LOG = (LOGDIR / f"driver_{RUN_TAG}.log").open("w", encoding="utf-8")

def log(*a):
    line = " ".join(str(x) for x in a)
    LOG.write(line + "\n"); LOG.flush()
    print(line, flush=True)

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

class Rec:
    def __init__(self): self.events = []; self.lock = threading.Lock()
    def __call__(self, e):
        with self.lock: self.events.append(e)
    def of(self, t):
        with self.lock: return [e for e in self.events if e.get("type") == t]
    def running_task_ids(self):
        with self.lock:
            return sorted({e.get("taskId") for e in self.events
                           if e.get("type") == "status" and e.get("status") == "running"
                           and e.get("taskId") is not None})

def goal_events(data_root):
    rows = []
    path = Path(data_root) / "logs" / "goal_events.jsonl"
    if not path.is_file():
        return rows
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except Exception:
            continue
    return rows

def wait_idle(agent, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if not getattr(agent, "is_running", False):
            return True
        time.sleep(0.1)
    return False

def run_turn(agent, prompt, timeout):
    out = agent.put_task(prompt)
    done = None; events = []
    end = time.time() + timeout
    while time.time() < end:
        try:
            ev = out.get(timeout=0.3); events.append(ev)
            if isinstance(ev, dict) and "done" in ev:
                done = ev; break
        except Exception:
            if not getattr(agent, "is_running", False) and events:
                break
    wait_idle(agent, 30)
    text = str((done or {}).get("done") or "")
    return {"ok": bool(done), "text": text[:400], "nEvents": len(events)}

def _done_ids(recorder, ids):
    with recorder.lock:
        return {e.get("taskId") for e in recorder.events
                if e.get("type") == "assistant_done" and e.get("taskId") in ids}

def _wait_new_wake_turn(recorder, before_ids, timeout):
    """Wait until a task id outside before_ids has both started and finished."""
    end = time.time() + timeout
    while time.time() < end:
        new_ids = set(recorder.running_task_ids()) - before_ids
        if new_ids and _done_ids(recorder, new_ids):
            return sorted(new_ids)
        time.sleep(0.3)
    return []

def main() -> int:
    setup_env()
    TARGET.mkdir(parents=True, exist_ok=True)
    from frontends.ink.bridge import GenericAgentBridge
    recorder = Rec()
    summary = {"target": str(TARGET), "dataRoot": str(DATA),
               "model": "default/deepseek-v4.1-flash", "phases": {}}
    bridge = agent = None
    try:
        bridge = GenericAgentBridge(emit=recorder)
        agent = bridge.agent
        threading.Thread(target=agent.run, daemon=True).start()
        bridge.model_switch("default/deepseek-v4.1-flash")
        time.sleep(2)

        # ---------- Phase A: default OFF must not schedule a wake ----------
        os.environ.pop("GA_GOAL_WAKE", None)
        r1 = run_turn(agent, "/goal 主目录下必须存在文件 wake_done.txt 且内容包含 WAKE_OK", 60)
        summary["phases"]["A_set_goal"] = r1
        rec("A: /goal set an active goal", getattr(agent, "active_goal", None) is not None)
        before = set(recorder.running_task_ids())
        time.sleep(4)
        rec("A: default-off leaves no scheduler", getattr(agent, "_goal_wake", None) is None)
        rec("A: default-off scheduled no wake turn",
            not (set(recorder.running_task_ids()) - before))

        # ---------- Phase B: retry wake (real new turn) ----------
        os.environ["GA_GOAL_WAKE"] = "1"
        if getattr(agent, "active_goal", None) is None:
            run_turn(agent, "/goal 主目录下必须存在文件 wake_done.txt 且内容包含 WAKE_OK", 60)
        before = set(recorder.running_task_ids())
        sched = agent._ensure_goal_wake()
        rec("B: scheduler created when enabled + callback present",
            sched is not None, type(sched).__name__ if sched else "None")
        armed = sched.arm_retry(cause="simulated provider drop", delay_ms=1500) if sched else False
        rec("B: arm_retry accepted", armed)
        newB = _wait_new_wake_turn(recorder, before, 120)
        summary["phases"]["B_retry"] = {"new_task_ids": newB}
        rec("B: retry produced a real new turn", bool(newB), str(newB))
        # A retry wake that the model then satisfies is a SUCCESS: the goal ends
        # met (cleared) rather than staying active. Only a spurious/dropped goal
        # (cleared without being met) would be a failure.
        gevents = goal_events(DATA)
        fired = [e for e in gevents if e.get("event") == "goal_interruption"
                 and e.get("outcome") == "fired"]
        met = [e for e in gevents if e.get("event") == "goal_met" and e.get("met")]
        rec("B: retry wake delivered (goal_interruption fired event)",
            len(fired) >= 1, str(len(fired)) + " fired")
        rec("B: goal either still active or legitimately met (not dropped)",
            getattr(agent, "active_goal", None) is not None or len(met) >= 1,
            "active" if getattr(agent, "active_goal", None) is not None else "met")

        # ---------- Phase C: check-in wake (real new turn) ----------
        if getattr(agent, "active_goal", None) is None:
            run_turn(agent, "/goal 主目录下必须存在文件 wake_done.txt 且内容包含 WAKE_OK", 60)
        before = set(recorder.running_task_ids())
        sched = agent._ensure_goal_wake()
        armed_c = sched.arm_checkin(delay_ms=1500) if sched else False
        rec("C: arm_checkin accepted", armed_c)
        newC = _wait_new_wake_turn(recorder, before, 120)
        summary["phases"]["C_checkin"] = {"new_task_ids": newC}
        rec("C: check-in produced a real new turn", bool(newC), str(newC))

    except Exception as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
        summary["traceback"] = traceback.format_exc()
        log("ERROR", summary["error"]); log(summary["traceback"])
    finally:
        os.environ.pop("GA_GOAL_WAKE", None)
        try:
            if agent is not None:
                setattr(agent, "stop_sig", True)
                try: agent.abort()
                except Exception: pass
        except Exception: pass

    out = LOGDIR / f"summary_{RUN_TAG}.json"
    out.write_text(json.dumps({"summary": summary, "checks": CHECKS}, ensure_ascii=False, indent=2),
                   encoding="utf-8", newline="\n")
    log("SUMMARY ->", str(out))
    npass = sum(1 for c in CHECKS if c["ok"])
    log(f"RESULT {npass}/{len(CHECKS)} checks passed")
    return 0 if CHECKS and npass == len(CHECKS) else 1

if __name__ == "__main__":
    sys.exit(main())
