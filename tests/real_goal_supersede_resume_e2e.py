"""Real-LLM smoke: replacing a goal then resuming restores the NEW goal only.

Flow: warm a turn, set goal A, set goal B (supersede), restore into a fresh
agent, and assert the restored condition is B (not A), origin=restored,
iterations=0, and exactly one goal:stop hook.
"""
from __future__ import annotations
import json, os, sys, time, threading, traceback
from pathlib import Path

REPO = Path.cwd()
for p in (REPO, REPO / "frontends"):
    sys.path.insert(0, str(p))

RUN_TAG = time.strftime("%Y%m%d-%H%M%S")
TARGET = Path("D:/test20260416") / f"_goal_super_{RUN_TAG}"
DATA = REPO / "temp" / "goal_super" / f"data_{RUN_TAG}"
LOGDIR = REPO / "temp" / "goal_super"
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
        "GA_PROJECT_ROOT": str(TARGET), "GA_DATA_DIR": str(DATA),
        "GA_WORKFLOW_WORKSPACE_ROOT": str(TARGET),
        "GA_LAUNCH_CWD": str(TARGET), "GA_ORIGINAL_CWD": str(TARGET),
        "GA_LLM_CONFIG": str(REPO / "llm.yaml"),
    })
    os.environ.pop("GA_HOOKS_DISABLED", None); os.environ.pop("GA_GOAL_NUDGE", None)

class Rec:
    def __init__(self): self.events = []
    def __call__(self, e): self.events.append(e)

def wait_idle(agent, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if not getattr(agent, "is_running", False): return True
        time.sleep(0.1)
    return False

def run_turn(agent, prompt, timeout):
    out = agent.put_task(prompt); done = None; end = time.time() + timeout; t0 = time.time()
    while time.time() < end:
        try:
            ev = out.get(timeout=0.3)
            if isinstance(ev, dict) and "done" in ev: done = ev; break
        except Exception:
            if not getattr(agent, "is_running", False): break
    wait_idle(agent, 30)
    return {"ok": bool(done), "text": str((done or {}).get("done") or "")[:200], "secs": round(time.time()-t0,1)}

def main() -> int:
    setup_env(); TARGET.mkdir(parents=True, exist_ok=True)
    from frontends.ink.bridge import GenericAgentBridge
    from frontends.terminal import continue_cmd
    summary = {"target": str(TARGET), "model": "default/deepseek-v4.1-flash"}
    bridge = agent = rest_agent = None
    try:
        bridge = GenericAgentBridge(emit=Rec()); agent = bridge.agent
        threading.Thread(target=agent.run, daemon=True).start()
        bridge.model_switch("default/deepseek-v4.1-flash"); time.sleep(2)

        r0 = run_turn(agent, "请在当前工作目录创建文件 scratch_note.txt，内容写 SCRATCH_OK，然后结束。", 300)
        summary["warmup"] = r0; rec("warmup turn completed", r0["ok"], r0["text"][:80])

        goal_a = "条件A：必须存在文件 a_marker.txt"
        goal_b = "条件B：必须存在文件 b_marker.txt"
        run_turn(agent, f"/goal {goal_a}", 60)
        rec("goal A set", getattr(agent.active_goal, "condition", None) == goal_a)
        run_turn(agent, f"/goal {goal_b}", 60)
        rec("goal B set (supersedes A)", getattr(agent.active_goal, "condition", None) == goal_b)

        path = getattr(agent, "session_path", None)
        rest_agent = bridge.agent_factory()
        msg, _ = continue_cmd.restore(rest_agent, path)
        summary["restore_msg"] = msg
        rec("restore reported ok", str(msg).startswith(("✓", "✅")), msg)
        g = getattr(rest_agent, "active_goal", None)
        rec("restored goal is B not A", g is not None and g.condition == goal_b, getattr(g, "condition", None))
        if g is not None:
            rec("origin restored", str(g.origin) == "restored", g.origin)
            rec("iterations reset", int(g.iterations) == 0, g.iterations)
        rt = getattr(rest_agent, "hook_runtime", None)
        keys = [e.get("key") for e in rt.list_session_hooks("Stop")] if rt is not None else []
        rec("exactly one goal:stop hook", keys.count("goal:stop") == 1, keys)
    except Exception as exc:
        summary["error"] = f"{type(exc).__name__}: {exc}"
        summary["traceback"] = traceback.format_exc()
        log("ERROR", summary["error"]); log(summary["traceback"])
    finally:
        for a in (agent, rest_agent):
            try:
                if a is not None:
                    setattr(a, "stop_sig", True)
                    try: a.abort()
                    except Exception: pass
            except Exception: pass
    out = LOGDIR / f"summary_{RUN_TAG}.json"
    out.write_text(json.dumps({"summary": summary, "checks": CHECKS}, ensure_ascii=False, indent=2),
                   encoding="utf-8", newline="\n")
    npass = sum(1 for c in CHECKS if c["ok"])
    log("SUMMARY ->", str(out)); log(f"RESULT {npass}/{len(CHECKS)} checks passed")
    return 0 if npass == len(CHECKS) and CHECKS else 1

if __name__ == "__main__":
    sys.exit(main())
