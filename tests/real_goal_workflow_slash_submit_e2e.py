"""Real-LLM proof: "/goal ... 使用 workflow" through the REAL Ink bridge.submit path.

Root cause regression (2026-11-03): ``bridge.submit`` prefixed the user's
``/goal ...`` with a "[WORKFLOW OPT-IN]" reminder *before* queuing it, so
``GenericAgent.run`` saw a string that did not start with "/", ``_handle_slash_cmd``
passed it through untouched, and the goal was never armed — the workflow still
ran, but no goal card, no met badge, no Stop-gate evaluation ever happened.

This e2e drives the *same* entry point a user hits in the Ink UI
(``bridge.submit``) with a genuine workflow opt-in prompt, and asserts:

  1. the goal actually gets armed (``agent.active_goal`` set) — the bug;
  2. the turn that launches a background workflow ends normally and defers the
     goal (no spin / no premature evaluation);
  3. the workflow reaches a terminal status and writes its marker;
  4. after the workflow handoff turn, a NON-deferred goal_status appears
     (the goal is actually evaluated, not left dangling).

Real default model (default/deepseek-v4.1-flash). Records are written to
temp/goal_wf_slash_submit/ (git-ignored run artifacts).
"""
from __future__ import annotations
import json, os, sys, time, threading, traceback
from pathlib import Path

REPO = Path.cwd()
for p in (REPO, REPO / "frontends"):
    sys.path.insert(0, str(p))

TAG = time.strftime("%Y%m%d-%H%M%S")
TARGET = Path("D:/test20260416") / f"_goal_wf_slash_{TAG}"
DATA = REPO / "temp" / "goal_wf_slash_submit" / f"data_{TAG}"
LOGDIR = REPO / "temp" / "goal_wf_slash_submit"; LOGDIR.mkdir(parents=True, exist_ok=True)
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

        marker = TARGET / "wf_ok.txt"
        # Genuine workflow opt-in phrasing (matches the real failing user input).
        user_text = (
            f"/goal 使用workflow 在目录 {TARGET} 下创建文件 wf_ok.txt，内容必须正好是 WF_OK。"
            "只做这一件事，完成后总结。"
        )
        summary["user_text"] = user_text
        task_id = bridge.submit(user_text)
        summary["task_id"] = task_id

        # Wait until the goal is armed OR the turn ends (goal must be armed almost
        # immediately from the slash command, before any model call).
        end = time.time() + 30
        armed = False
        while time.time() < end:
            if getattr(agent, "active_goal", None) is not None:
                armed = True; break
            time.sleep(0.2)
        summary["goal_condition"] = getattr(getattr(agent, "active_goal", None), "condition", None)
        rec("goal armed via bridge.submit('/goal ... 使用 workflow')", armed, summary["goal_condition"])

        # Let the first turn run (loads skill, authors & launches workflow).
        wait_idle(agent, 240)
        # A background workflow makes the launch turn's Stop gate *defer* goal
        # evaluation (never judge the pre-handoff transcript). This is inherently
        # racy: if the workflow finishes before the launch turn stops, the handoff
        # coalesces and the goal is judged (met) in the same turn instead. So the
        # robust, user-visible invariant is: no premature "not met" evaluation
        # that would spin the agent. Record defer as informational.
        summary["last_goal_defer"] = getattr(agent.handler, "last_goal_defer", None)
        launcher_gs = goal_status_events(getattr(agent, "session_path", None))
        summary["saw_deferred_event"] = any(str(g.get("reason")) == "deferred" for g in launcher_gs)
        premature_neg = [
            g for g in launcher_gs
            if str(g.get("reason")) != "deferred" and g.get("met") is False
            and not g.get("sentinel")
        ]
        summary["premature_negative"] = len(premature_neg)
        rec("no premature negative goal evaluation at launch stop (no spin)",
            len(premature_neg) == 0, "premature_neg=%d deferred_event=%s" % (
                len(premature_neg), summary["saw_deferred_event"]))

        # Wait for the workflow terminal status.
        wf = None; end = time.time() + 300
        while time.time() < end:
            runs = bridge.workflow_store.list_runs(session_id=str(getattr(agent, "session_id", "")))
            if runs:
                run = runs[-1]
                summary["run_id"] = run.run_id
                if run.status in {"succeeded","degraded","failed","cancelled","killed","interrupted"}:
                    wf = {"runId": run.run_id, "status": run.status}
                    break
            time.sleep(2)
        summary["workflow"] = wf
        rec("workflow reached a terminal status",
            wf is not None and wf["status"] in {"succeeded","degraded"}, wf)
        rec("workflow wrote wf_ok.txt into the goal directory",
            marker.is_file() and "WF_OK" in marker.read_text(encoding="utf-8", errors="replace"),
            str(marker))

        # Wait for the handoff turn to run and the goal to be evaluated.
        time.sleep(3); wait_idle(agent, 180)
        gs = []; end = time.time() + 240
        while time.time() < end:
            gs = goal_status_events(getattr(agent, "session_path", None))
            if any(str(g.get("reason")) != "deferred" for g in gs):
                break
            time.sleep(2)
        summary["goal_status"] = gs[-4:]
        nondef = [g for g in gs if str(g.get("reason")) != "deferred"]
        met_events = [g for g in nondef if g.get("met") is True]
        rec("goal actually evaluated after workflow and reached met=True",
            len(met_events) >= 1,
            json.dumps(met_events[-1:], ensure_ascii=False)[:300])
        summary["goal_active_end"] = agent.active_goal is not None
        summary["goal_cleared_or_active"] = ("cleared" if agent.active_goal is None else "still-active")

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
