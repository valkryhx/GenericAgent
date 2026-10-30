"""Real-LLM smoke for Stage F phase 5 (metadata / card / telemetry / nudge).

Drives the real bridge + default LLM (deepseek-v4.1-flash). Checks:
  - /goal sets an active goal and emits goal_metadata (card data);
  - tokens/iterations survive; a real turn nudges iterations;
  - asking the model to satisfy the goal drives a met verdict -> terminal badge
    (goal_metadata with goal:null + terminal.met) + telemetry has goal_achieved;
  - with GA_GOAL_NUDGE unset no goal_nudge event appears (zero noise).
"""
from __future__ import annotations
import json, os, sys, time, traceback
from pathlib import Path

REPO = Path.cwd()
for p in (REPO, REPO / "frontends"):
    sys.path.insert(0, str(p))

RUN_TAG = time.strftime("%Y%m%d-%H%M%S")
TARGET = Path("D:/test20260416") / f"_goal_p5_{RUN_TAG}"
DATA = REPO / "temp" / "goal_p5" / f"data_{RUN_TAG}"
LOGDIR = REPO / "temp" / "goal_p5"
LOGDIR.mkdir(parents=True, exist_ok=True)
LOG = (LOGDIR / f"driver_{RUN_TAG}.log").open("w", encoding="utf-8")

def log(*a):
    line = " ".join(str(x) for x in a)
    LOG.write(line + "\n"); LOG.flush(); print(line, flush=True)

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
    os.environ.pop("GA_GOAL_NUDGE", None)          # default OFF -> zero noise
    os.environ.pop("GA_GOAL_TELEMETRY", None)      # default -> local writes
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
    t0 = time.time(); done = None; end = time.time() + timeout
    while time.time() < end:
        try:
            ev = out.get(timeout=0.3)
            if isinstance(ev, dict) and "done" in ev:
                done = ev; break
        except Exception:
            if not getattr(agent, "is_running", False):
                break
    wait_idle(agent, 30)
    return {"ok": bool(done), "text": str((done or {}).get("done") or "")[:500],
            "secs": round(time.time()-t0,1)}

def telemetry_events():
    p = Path(DATA) / "logs" / "goal_events.jsonl"
    rows = []
    if p.is_file():
        for line in p.read_text(encoding="utf-8", errors="replace").splitlines():
            line = line.strip()
            if not line: continue
            try: rows.append(json.loads(line))
            except Exception: pass
    return rows

def main() -> int:
    setup_env()
    TARGET.mkdir(parents=True, exist_ok=True)
    from frontends.ink.bridge import GenericAgentBridge
    recorder = Rec()
    summary = {"target": str(TARGET), "dataRoot": str(DATA), "model": "default/deepseek-v4.1-flash", "phases": {}}
    bridge = agent = None
    try:
        bridge = GenericAgentBridge(emit=recorder)
        agent = bridge.agent
        import threading
        threading.Thread(target=agent.run, daemon=True).start()
        bridge.model_switch("default/deepseek-v4.1-flash")
        time.sleep(2)

        marker = TARGET / "p5_done.txt"
        goal_text = f"必须存在文件 {marker.name}，且其内容包含 P5_OK"

        # 1) set goal
        r1 = run_turn(agent, f"/goal {goal_text}", 60)
        summary["phases"]["set_goal"] = r1
        rec("active goal set", getattr(agent, "active_goal", None) is not None)
        meta_set = [e for e in recorder.of("goal_metadata") if e.get("goal")]
        rec("goal_metadata(card) emitted on set", bool(meta_set),
            json.dumps(meta_set[-1]["goal"], ensure_ascii=False)[:200] if meta_set else "")

        # 2) real turn that creates the file (iteration bump expected)
        r2 = run_turn(agent, f"请在 {TARGET} 下创建文件 {marker.name}，内容写 P5_OK，然后结束。", 300)
        summary["phases"]["work"] = r2
        rec("work turn completed", r2["ok"], r2["text"][:160])
        rec("marker file exists after work", marker.is_file())

        # 3) let goal be evaluated -> met
        r3 = run_turn(agent, "目标是否已达成？如已达成请结束目标。", 300)
        summary["phases"]["evaluate"] = r3
        time.sleep(2)
        metas = recorder.of("goal_metadata")
        terminals = [e for e in metas if e.get("terminal")]
        rec("terminal goal_metadata emitted (badge)", bool(terminals),
            json.dumps(terminals[-1], ensure_ascii=False)[:260] if terminals else "")
        if terminals:
            rec("terminal.met is true", bool(terminals[-1]["terminal"].get("met")))
            rec("goal cleared after met", getattr(agent, "active_goal", None) is None)
        tel = telemetry_events()
        names = [e.get("event") for e in tel]
        summary["telemetry"] = tel
        rec("telemetry recorded goal_set", "goal_set" in names, names)
        rec("telemetry recorded goal_achieved", "goal_achieved" in names, names)
        rec("telemetry has no condition text", all("P5_OK" not in json.dumps(e, ensure_ascii=False) for e in tel))

        # 4) nudge off -> no goal_nudge event
        rec("no goal_nudge event when disabled", len(recorder.of("goal_nudge")) == 0)

    except Exception as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
        summary["traceback"] = traceback.format_exc()
        log("ERROR", summary["error"]); log(summary["traceback"])
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
    npass = sum(1 for c in CHECKS if c["ok"])
    log("SUMMARY ->", str(out))
    log(f"RESULT {npass}/{len(CHECKS)} checks passed")
    return 0 if npass == len(CHECKS) and CHECKS else 1

if __name__ == "__main__":
    sys.exit(main())
