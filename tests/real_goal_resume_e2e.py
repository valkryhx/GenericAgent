"""Real-LLM smoke for Phase 6 gap 1: goal survivability across resume.

Flow (default LLM = deepseek-v4.1-flash):
  1) set a /goal and run a real working turn so the transcript persists an
     ``active_goal`` event plus a working_state with session_output_tokens;
  2) build a FRESH agent and restore that session via the same helper the
     bridge uses (continue_cmd.restore -> restore_agent_session);
  3) assert the restored agent has the active goal back, the ``goal:stop`` hook
     re-armed, session_output_tokens restored, and a goal_metadata snapshot is
     emitted even though nothing changed (force snapshot).
"""
from __future__ import annotations
import json, os, sys, time, threading, traceback
from pathlib import Path

REPO = Path.cwd()
for p in (REPO, REPO / "frontends"):
    sys.path.insert(0, str(p))

RUN_TAG = time.strftime("%Y%m%d-%H%M%S")
TARGET = Path("D:/test20260416") / f"_goal_resume_{RUN_TAG}"
DATA = REPO / "temp" / "goal_resume" / f"data_{RUN_TAG}"
LOGDIR = REPO / "temp" / "goal_resume"
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
    os.environ.pop("GA_GOAL_NUDGE", None)
    os.environ.pop("GA_GOAL_TELEMETRY", None)

class Rec:
    def __init__(self): self.events = []
    def __call__(self, e): self.events.append(e)
    def of(self, t): return [e for e in self.events if e.get("type") == t]

def wait_idle(agent, timeout):
    end = time.time() + timeout
    while time.time() < end:
        if not getattr(agent, "is_running", False):
            return True
        time.sleep(0.1)
    return False

def run_turn(agent, prompt, timeout):
    out = agent.put_task(prompt)
    done = None; end = time.time() + timeout; t0 = time.time()
    while time.time() < end:
        try:
            ev = out.get(timeout=0.3)
            if isinstance(ev, dict) and "done" in ev:
                done = ev; break
        except Exception:
            if not getattr(agent, "is_running", False):
                break
    wait_idle(agent, 30)
    return {"ok": bool(done), "text": str((done or {}).get("done") or "")[:400],
            "secs": round(time.time()-t0,1)}

def main() -> int:
    setup_env()
    TARGET.mkdir(parents=True, exist_ok=True)
    from frontends.ink.bridge import GenericAgentBridge
    from frontends.terminal import continue_cmd
    summary = {"target": str(TARGET), "dataRoot": str(DATA), "model": "default/deepseek-v4.1-flash"}
    bridge = None; agent = None; rest_agent = None
    try:
        bridge = GenericAgentBridge(emit=Rec())
        agent = bridge.agent
        threading.Thread(target=agent.run, daemon=True).start()
        bridge.model_switch("default/deepseek-v4.1-flash")
        time.sleep(2)

        # 1) A plain working turn FIRST accrues real output tokens (no goal yet).
        other = TARGET / "scratch_note.txt"
        r0 = run_turn(agent, f"请在当前工作目录创建文件 {other.name}，内容写 SCRATCH_OK，然后结束。", 300)
        summary["warmup"] = r0
        rec("warmup turn completed", r0["ok"], r0["text"][:120])

        # 2) Set the goal now (tokens_at_start = current cumulative output tokens).
        #    This is a local slash command: it does NOT run the model, so the goal
        #    stays active until the next evaluated turn -- which we avoid.
        goal_text = "必须存在文件 goal_marker.txt（位于工作目录），内容包含 GOAL_OK"
        r1 = run_turn(agent, f"/goal {goal_text}", 60)
        summary["set_goal"] = r1
        rec("goal set on first agent", getattr(agent, "active_goal", None) is not None)
        rec("goal still active before resume", getattr(agent, "active_goal", None) is not None)

        path = getattr(agent, "session_path", None)
        rec("session path present", bool(path), str(path))
        first_tokens = int(getattr(getattr(agent.llmclient, "backend", None), "session_output_tokens", 0) or 0)
        summary["first_session_output_tokens"] = first_tokens
        rec("session_output_tokens accrued", first_tokens > 0, first_tokens)

        # Restore into a FRESH agent using the same helper the bridge uses.
        rest_agent = bridge.agent_factory()
        msg, _ = continue_cmd.restore(rest_agent, path)
        summary["restore_msg"] = msg
        rec("restore reported ok", str(msg).startswith(("✓", "✅")), msg)
        g = getattr(rest_agent, "active_goal", None)
        rec("active goal restored", g is not None, getattr(g, "condition", None))
        if g is not None:
            rec("restored condition matches", g.condition == goal_text)
            # CC re-arms fresh: iterations reset to 0, origin=restored, and the
            # token anchor is the CURRENT cumulative output tokens.
            rec("iterations reset to 0 (CC re-arm)", int(g.iterations) == 0, g.iterations)
            rec("origin is restored", str(g.origin) == "restored", g.origin)
            rec("tokens_at_start = current cumulative", int(g.tokens_at_start) == first_tokens,
                f"{g.tokens_at_start} vs {first_tokens}")
        rt = getattr(rest_agent, "hook_runtime", None)
        keys = [e.get("key") for e in rt.list_session_hooks("Stop")] if rt is not None else []
        rec("goal:stop hook re-armed", "goal:stop" in keys, keys)
        restored_tokens = int(getattr(getattr(rest_agent.llmclient, "backend", None), "session_output_tokens", 0) or 0)
        rec("session_output_tokens restored", restored_tokens == first_tokens, f"{restored_tokens} vs {first_tokens}")

        # Force snapshot on the restored agent (what the bridge does on ready/resume).
        rec_events = []
        rest_agent.hook_emit = rec_events.append
        rest_agent.emit_goal_snapshot()
        metas = [e for e in rec_events if e.get("type") == "goal_metadata"]
        rec("restored goal_metadata snapshot emitted", bool(metas) and metas[-1].get("goal") is not None,
            json.dumps(metas[-1], ensure_ascii=False)[:220] if metas else "")
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
    log("SUMMARY ->", str(out))
    log(f"RESULT {npass}/{len(CHECKS)} checks passed")
    return 0 if npass == len(CHECKS) and CHECKS else 1

if __name__ == "__main__":
    sys.exit(main())
