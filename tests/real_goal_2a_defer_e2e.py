"""Real-LLM proof: Stage 2-A defers goal evaluation while a background sub-agent runs.

Flow:
  1. main turn: /goal <condition>  (sets active goal via slash command)
  2. main turn: ask the agent to spawn a SLOW background sub-agent that writes a
     marker file after a deliberate delay (sleep), so it is still in flight when
     the main turn ends. The main turn must NOT be blocked by the goal gate.
  3. inspect transcript jsonl: expect goal_status(reason='deferred').
  4. wait for subagent to finish; then final main turn asks goal to be judged;
     the goal should evaluate normally (not deferred) since nothing is in flight.
"""
from __future__ import annotations
import json, os, sys, time, traceback
from pathlib import Path

REPO = Path.cwd()
for p in (REPO, REPO / "frontends"):
    sys.path.insert(0, str(p))

RUN_TAG = time.strftime("%Y%m%d-%H%M%S")
TARGET = Path("D:/test20260416") / f"_goal_2a_{RUN_TAG}"
DATA = REPO / "temp" / "goal_2a" / f"data_{RUN_TAG}"
LOGDIR = REPO / "temp" / "goal_2a"
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
    def __init__(self): self.events = []
    def __call__(self, e): self.events.append(e)
    def of(self, t): return [e for e in self.events if e.get("type") == t]
    def last(self, t):
        r = self.of(t); return r[-1] if r else None

def wait_idle(agent, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if not getattr(agent, "is_running", False):
            return True
        time.sleep(0.1)
    return False

def run_turn(agent, prompt, timeout):
    out = agent.put_task(prompt)
    t0 = time.time()
    # put_task returns the display queue; drain it until a done event or timeout
    events = []
    done = None
    end = time.time() + timeout
    while time.time() < end:
        try:
            ev = out.get(timeout=0.3)
            events.append(ev)
            if isinstance(ev, dict) and "done" in ev:
                done = ev
                break
        except Exception:
            if not getattr(agent, "is_running", False) and events:
                break
    wait_idle(agent, 30)
    text = ""
    if done: text = str(done.get("done") or "")
    if not text:
        for ev in reversed(events):
            if isinstance(ev, dict) and ev.get("done"):
                text = str(ev.get("done")); break
    return {"ok": bool(done), "text": text[:600], "secs": round(time.time()-t0,1),
            "nEvents": len(events)}

def goal_status_events(path):
    rows = []
    if not path or not Path(path).is_file():
        return rows
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line: continue
        try: o = json.loads(line)
        except Exception: continue
        if isinstance(o, dict) and o.get("type") == "goal_status":
            rows.append(o)
    return rows

def main() -> int:
    setup_env()
    TARGET.mkdir(parents=True, exist_ok=True)
    from frontends.ink.bridge import GenericAgentBridge
    recorder = Rec()
    summary = {"target": str(TARGET), "dataRoot": str(DATA), "model": "default/deepseek-v4.1-flash", "phases": {}}
    bridge = None
    agent = None
    try:
        bridge = GenericAgentBridge(emit=recorder)
        agent = bridge.agent
        # start agent loop in a thread
        import threading
        th = threading.Thread(target=agent.run, daemon=True)
        th.start()
        bridge.model_switch("default/deepseek-v4.1-flash")
        time.sleep(2)

        marker = TARGET / "child_done.txt"
        child = TARGET / "child_task.txt"

        # ---- turn 1: set goal ----
        goal_text = "主目录下必须存在文件 child_done.txt 且其内容包含 CHILD_OK"
        r1 = run_turn(agent, f"/goal {goal_text}", 60)
        summary["phases"]["set_goal"] = r1
        log("active_goal after set:", getattr(agent, "active_goal", None))
        rec("turn1 /goal set an active goal", getattr(agent, "active_goal", None) is not None,
            getattr(getattr(agent, "active_goal", None), "condition", None))

        # ---- turn 2: spawn a slow background sub-agent ----
        spawn_prompt = (
            f"Use the spawn_agent tool to start ONE background sub-agent named 'slowchild'. "
            f"Tell it to run exactly this shell command with code_run: "
            f"\"python -c \\\"import time;time.sleep(25);open(r'{marker}','w',encoding='utf-8').write('CHILD_OK')\\\"\" "
            f"and then finish. Do not wait for the sub-agent to complete yourself; spawn it and "
            f"immediately stop your turn after reporting the spawn status. Do not create child_done.txt yourself."
        )
        r2 = run_turn(agent, spawn_prompt, 300)
        summary["phases"]["spawn"] = r2
        # give a beat for the stop gate to have run at turn end
        time.sleep(3)
        rec("turn2 (spawn) completed a turn", r2["ok"], r2["text"][:200])

        # check transcript for deferred goal_status
        sp = getattr(agent, "session_path", None)
        gs = goal_status_events(sp)
        summary["session_path"] = str(sp)
        summary["goal_status_events_after_spawn"] = gs
        deferred = [g for g in gs if str(g.get("reason")) == "deferred"]
        rec("transcript has goal_status(reason='deferred') from the spawn turn",
            len(deferred) >= 1, json.dumps(deferred[:2], ensure_ascii=False)[:300])
        rec("active goal is still set after deferred turn (not cleared/failed)",
            getattr(agent, "active_goal", None) is not None,
            getattr(getattr(agent, "active_goal", None), "iterations", None))

        # ---- wait for the sub-agent to actually finish ----
        end = time.time() + 120
        while time.time() < end and not marker.is_file():
            time.sleep(1)
        rec("background sub-agent eventually wrote child_done.txt", marker.is_file(),
            str(marker) + (" exists" if marker.is_file() else " missing"))
        if marker.is_file():
            summary["phases"]["child_content"] = marker.read_text(encoding="utf-8", errors="replace")[:80]

        # ---- turn 3: ask goal to be evaluated now (nothing in flight) ----
        r3 = run_turn(agent, "看一下当前目标是否已经达成，如已达成请结束目标。", 300)
        summary["phases"]["evaluate"] = r3
        time.sleep(2)
        gs2 = goal_status_events(sp)
        summary["goal_status_events_final"] = gs2
        # after real completion the goal should be met (or at least a non-deferred verdict recorded)
        nondef = [g for g in gs2 if str(g.get("reason")) != "deferred"]
        rec("after child finished, goal evaluated (a non-deferred goal_status exists)",
            len(nondef) >= 1, json.dumps(nondef[-2:], ensure_ascii=False)[:400])

    except Exception as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
        summary["traceback"] = traceback.format_exc()
        log("ERROR", summary["error"])
        log(summary["traceback"])
    finally:
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
    return 0 if npass == len(CHECKS) and CHECKS else 1


if __name__ == "__main__":
    sys.exit(main())
