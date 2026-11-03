"""Real-LLM proof: "/goal ... 使用 agent team" through the REAL Ink bridge.submit path.

Two independent root causes made goal+team silently not arm the goal:

1. (fixed earlier) ``bridge.submit`` prepended the "[TEAM/WORKFLOW OPT-IN]"
   reminder to the user text *before* queuing, hiding a leading "/goal".
2. (this run) the Ink composer can deliver "/goal ..." with *leading whitespace*,
   so after the model_prefix was re-attached the text was "...\n\n /goal ...".
   The old ``raw_query.startswith('/')`` check (no strip) missed it.

This e2e drives the real entry point with a leading space before "/goal", and
asserts the goal is actually armed, the team executes, and the goal is finally
evaluated to met=True.

Real default model (default/deepseek-v4.1-flash). Artifacts under temp/goal_team_slash_submit/.
"""
from __future__ import annotations
import json, os, sys, time, threading, traceback
from pathlib import Path

REPO = Path.cwd()
for p in (REPO, REPO / "frontends"):
    sys.path.insert(0, str(p))

TAG = time.strftime("%Y%m%d-%H%M%S")
TARGET = Path("D:/test20260416") / f"_goal_team_slash_{TAG}"
DATA = REPO / "temp" / "goal_team_slash_submit" / f"data_{TAG}"
LOGDIR = REPO / "temp" / "goal_team_slash_submit"; LOGDIR.mkdir(parents=True, exist_ok=True)
LOG = (LOGDIR / f"run_{TAG}.log").open("w", encoding="utf-8")

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

def main() -> int:
    setup_env(); TARGET.mkdir(parents=True, exist_ok=True)
    from frontends.ink.bridge import GenericAgentBridge
    rec_ev = Rec()
    summary = {"target": str(TARGET), "model": "default/deepseek-v4.1-flash"}
    bridge = agent = None
    try:
        bridge = GenericAgentBridge(emit=rec_ev); agent = bridge.agent
        threading.Thread(target=agent.run, daemon=True).start()
        bridge.model_switch("default/deepseek-v4.1-flash"); time.sleep(2)

        # Force the team session switch, then submit WITH a leading space on /goal
        # (the exact shape that used to break command dispatch).
        bridge._team_activation.enabled = True
        user_text = (
            " /goal 使用 agent team 并行完成：在 " + str(TARGET) +
            " 下生成三份互不相关的资料文件 a.md（讲 Python 列表推导式）、"
            "b.md（讲字典推导式）、c.md（讲生成器表达式），最后再写 index.md 汇总这三个文件的标题。"
        )
        summary["user_text"] = user_text
        task_id = bridge.submit(user_text)
        summary["task_id"] = task_id

        end = time.time() + 30; armed = False
        while time.time() < end:
            if getattr(agent, "active_goal", None) is not None:
                armed = True; break
            time.sleep(0.2)
        summary["goal_condition"] = getattr(getattr(agent, "active_goal", None), "condition", None)
        rec("goal armed via bridge.submit(' /goal ... agent team') [leading space]",
            armed, summary["goal_condition"])

        # Let the team run (spawn/wait/close can take a few minutes).
        end = time.time() + 420
        while time.time() < end:
            if not getattr(agent, "is_running", False):
                # give a handoff/eval turn a chance to start
                time.sleep(3)
                if not getattr(agent, "is_running", False):
                    break
            time.sleep(2)

        # verify the three files + index exist
        files = {n: (TARGET / n) for n in ("a.md", "b.md", "c.md", "index.md")}
        present = {n: (p.is_file() and p.stat().st_size > 0) for n, p in files.items()}
        summary["files"] = present
        rec("team wrote a.md/b.md/c.md and index.md (all non-empty)",
            all(present.values()), present)

        # wait for the goal to be evaluated
        gs = []; end = time.time() + 240
        while time.time() < end:
            gs = goal_status_events(getattr(agent, "session_path", None))
            if any(g.get("met") is True for g in gs):
                break
            time.sleep(2)
        summary["goal_status"] = gs[-4:]
        met = [g for g in gs if g.get("met") is True]
        rec("goal evaluated after team run and reached met=True", len(met) >= 1,
            json.dumps(met[-1:], ensure_ascii=False)[:300])
        summary["goal_active_end"] = agent.active_goal is not None

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
