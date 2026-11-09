"""Real-LLM regression: ONE ``/goal <condition>`` must start a working turn.

Bug it guards against: ``/goal`` used to set the goal and then return None, so
``GenericAgent.run()`` treated it as a finished local command (0 turns, <1s,
no model call, no files). The fix returns a kickoff prompt so the SAME /goal
message drives a real model turn, matching Claude Code's ``shouldQuery: true``
(/goal = type:query returning {value: "Goal set: ...", prompt: <kickoff>}).

Run:  python tests/real_goal_slash_kickoff_e2e.py
Exit: 0 = PASS, 1 = FAIL. Drives the real default LLM (moma/deepseek-latest-flash).
"""
from __future__ import annotations
import json, os, sys, time, threading, traceback
from pathlib import Path

REPO = Path.cwd()
for p in (REPO, REPO / "frontends"):
    sys.path.insert(0, str(p))

TAG = time.strftime("%Y%m%d-%H%M%S")
TARGET = (REPO / "temp" / "goal_kickoff" / f"run_{TAG}")
OUTDIR = REPO / "temp" / "goal_kickoff"; OUTDIR.mkdir(parents=True, exist_ok=True)
CHECKS = []
def rec(name, ok, detail=""):
    CHECKS.append({"name": name, "ok": bool(ok), "detail": str(detail)})
    print(("PASS " if ok else "FAIL ") + name + (" - " + str(detail) if detail else ""), flush=True)

def wait_idle(agent, t):
    end = time.time() + t
    while time.time() < end:
        if not getattr(agent, "is_running", False):
            return True
        time.sleep(0.1)
    return False

def main() -> int:
    TARGET.mkdir(parents=True, exist_ok=True)
    marker = TARGET / "hello_goal.txt"
    from frontends.ink.bridge import GenericAgentBridge
    from genericagent import paths
    ge = Path(paths.data_root()) / "logs" / "goal_events.jsonl"
    before = sum(1 for _ in ge.open(encoding="utf-8")) if ge.is_file() else 0

    events = []
    bridge = GenericAgentBridge(emit=lambda e: events.append(e))
    agent = bridge.agent
    threading.Thread(target=agent.run, daemon=True).start()
    try:
        bridge.model_switch("default/moma/deepseek-latest-flash")
    except Exception as e:
        print("model_switch warn:", e)
    time.sleep(2)

    cond = (f"在目录 {TARGET} 下创建一个名为 hello_goal.txt 的文件，内容恰好是 "
            f"HELLO_GOAL；创建后用 file_read 确认文件存在且内容正确")
    t0 = time.time()
    dq = agent.put_task("/goal " + cond, source="user")
    done = None; notes = []
    while time.time() - t0 < 300:
        try:
            item = dq.get(timeout=1.0)
        except Exception:
            if not getattr(agent, "is_running", False):
                break
            continue
        if isinstance(item, dict):
            if "system" in item:
                notes.append(item["system"])
            if "done" in item:
                done = item["done"]; break
    secs = round(time.time() - t0, 1)
    wait_idle(agent, 20)
    content = marker.read_text(encoding="utf-8", errors="replace") if marker.is_file() else ""
    evs = []
    if ge.is_file():
        for line in ge.read_text(encoding="utf-8", errors="replace").splitlines():
            try: evs.append(json.loads(line))
            except Exception: pass
    new_events = [e.get("event") for e in evs[before:]]

    rec("single /goal ran a real model turn (>2s)", secs > 2, f"{secs}s")
    rec("assistant produced a final message", bool((done or "").strip()), f"len={len(done or '')}")
    rec("'Goal set' notice surfaced (non-terminal)", any("Goal set:" in n for n in notes), notes[:2])
    rec("target file created", marker.is_file(), str(marker))
    rec("file content is HELLO_GOAL", content.strip() == "HELLO_GOAL", content[:40])
    rec("goal reached met", "goal_met" in new_events, new_events)
    rec("goal auto-cleared after met (active_goal None)", getattr(agent, "active_goal", None) is None)

    summary = {"target": str(TARGET), "elapsed_secs": secs, "model": "default/moma/deepseek-latest-flash",
               "assistant_len": len(done or ""), "goal_events_new": new_events, "checks": CHECKS}
    (OUTDIR / f"summary_{TAG}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                                encoding="utf-8", newline="\n")
    n = sum(1 for c in CHECKS if c["ok"])
    print(f"RESULT {n}/{len(CHECKS)} checks passed -> {OUTDIR / ('summary_' + TAG + '.json')}", flush=True)
    return 0 if CHECKS and n == len(CHECKS) else 1

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
