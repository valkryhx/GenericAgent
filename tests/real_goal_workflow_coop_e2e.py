"""Real-LLM proof: goal + background workflow actually cooperate.

Before the fix, an active goal could not see a background workflow (workflows
run on bridge launcher threads, not SubagentManager). The Stop gate evaluated
the pre-workflow transcript, blocked the turn, and spun the main agent for many
model steps in a single turn (probe showed 9 steps, done=False).

This e2e runs the REAL agent loop with the REAL default model
(default/deepseek-v4.1-flash) and asserts the end-to-end chain:

  1. /goal sets an active goal.
  2. Main turn launches a real background workflow -> the turn must END
     normally (done True), record goal_defer=True, and stay small (<= 8 steps).
  3. The workflow runs to a terminal status and writes its marker file.
  4. After the workflow finishes, the goal is evaluated in a later turn
     (a non-deferred goal_status appears), i.e. deferral is not permanent.

Fully self-contained: no external env required.
"""
from __future__ import annotations
import json, os, sys, time, threading, traceback
from pathlib import Path

REPO = Path.cwd()
for p in (REPO, REPO / "frontends"):
    sys.path.insert(0, str(p))

TAG = time.strftime("%Y%m%d-%H%M%S")
TARGET = Path("D:/test20260416") / f"_goal_wf_coop_{TAG}"
DATA = REPO / "temp" / "goal_wf_coop" / f"data_{TAG}"
LOGDIR = REPO / "temp" / "goal_wf_coop"; LOGDIR.mkdir(parents=True, exist_ok=True)
LOG = (LOGDIR / f"coop_{TAG}.log").open("w", encoding="utf-8")

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
            "text": str((done or {}).get("done") or "")[:300]}

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
    summary = {"target": str(TARGET), "dataRoot": str(DATA),
               "model": "default/deepseek-v4.1-flash"}
    bridge = agent = None
    try:
        bridge = GenericAgentBridge(emit=rec_ev); agent = bridge.agent
        threading.Thread(target=agent.run, daemon=True).start()
        bridge.model_switch("default/deepseek-v4.1-flash"); time.sleep(2)

        marker_abs = TARGET / "wf_out.txt"
        goal_text = f"目录 {TARGET} 下存在文件 wf_out.txt 且内容包含 WF_OK"
        r0 = run_turn(agent, f"/goal {goal_text}", 60)
        summary["set_goal"] = r0
        rec("turn1 /goal set an active goal", agent.active_goal is not None,
            getattr(agent.active_goal, "condition", None))

        # The workflow writes an ABSOLUTE path in the goal scope, so the goal is
        # actually achievable regardless of the run's own workspace.
        wf_script = (
            "export const meta = { name: 'coop-wf', description: 'write wf_out.txt to goal dir' }\n"
            "phase('write')\n"
            f"const a = await agent(`Write a file at the exact absolute path {marker_abs} "
            f"(use file_write). Its content must be exactly WF_OK. Do not write anywhere else.`, "
            "{ label: 'writer' })\n"
            "return { summary: a.summary }\n"
        )
        launch_prompt = (
            "请使用 workflow 工具在后台启动下面这段 workflow 脚本（script 参数），"
            "启动后只回报 run id 并结束本轮，不要等待它完成、不要调用 wait_agent：\n\n"
            + wf_script
        )
        r1 = run_turn(agent, launch_prompt, 180, hard_cap=150)
        summary["launch_turn"] = r1
        summary["last_goal_defer"] = getattr(agent.handler, "last_goal_defer", None)
        summary["goal_iterations_after_launch"] = getattr(agent.active_goal, "iterations", None)

        rec("launch turn ends normally (done=True) with goal active", r1["done"] is True, r1)
        rec("launch turn records goal_defer=True (workflow seen as in-flight)",
            summary["last_goal_defer"] is True, summary["last_goal_defer"])
        rec("launch turn does NOT spin (<= 8 model steps)", r1["steps"] <= 8, r1["steps"])
        rec("goal not mis-evaluated during launch (iterations stayed 0)",
            summary["goal_iterations_after_launch"] in (0, None),
            summary["goal_iterations_after_launch"])

        # wait for the workflow to reach a terminal status
        wf = None; end = time.time() + 300
        while time.time() < end:
            runs = bridge.workflow_store.list_runs(session_id=str(getattr(agent, "session_id", "")))
            if runs:
                run = runs[-1]
                summary["run_id"] = run.run_id
                if run.status in {"succeeded", "degraded", "failed", "cancelled", "killed", "interrupted"}:
                    wf = {"runId": run.run_id, "status": run.status,
                          "ws": (run.metadata or {}).get("workspacePath")}
                    break
            time.sleep(2)
        summary["workflow"] = wf
        rec("workflow reached a terminal status", wf is not None and wf["status"] in {"succeeded","degraded"},
            wf)
        rec("workflow wrote the marker file into the goal directory",
            marker_abs.is_file() and "WF_OK" in marker_abs.read_text(encoding="utf-8", errors="replace"),
            str(marker_abs))

        # the workflow handoff enqueues a fresh main turn; wait for it to run and
        # let the goal be evaluated there (no longer deferred).
        time.sleep(3)
        wait_idle(agent, 180)
        # wait until a non-deferred goal_status exists (met or not_met)
        gs = []
        end = time.time() + 180
        while time.time() < end:
            gs = goal_status_events(getattr(agent, "session_path", None))
            if any(str(g.get("reason")) != "deferred" for g in gs):
                break
            time.sleep(2)
        summary["goal_status"] = gs[-4:]
        nondef = [g for g in gs if str(g.get("reason")) != "deferred"]
        rec("goal evaluated after workflow finished (a non-deferred goal_status exists)",
            len(nondef) >= 1, json.dumps(nondef[-1:], ensure_ascii=False)[:300])
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
