"""Real-LLM *controlled* proof: goal defers while a workflow AND a team subagent
are both in flight, then evaluates once they settle.

This complements `real_goal_workflow_team_combo_e2e.py` (which is a pure
model-driven run). Here the two background channels are started deterministically
so the "both in flight at the same time" instant is reliably observable, while
every component is still real:
  - real bridge + real workflow Runtime (real child agent, real model)
  - real SubagentManager team_mode subagent (real child process, real model)
  - real goal Stop gate (`handler.run_stop_gate`) + real evaluator model

Assertions:
  1. right after launching both, workflow channel AND subagent channel are each
     in flight, and `has_in_flight_agents` is True.
  2. calling run_stop_gate while both run -> action 'none', last_goal_defer True,
     evaluator model NOT called, transcript records goal_status(reason=deferred).
  3. after both settle -> has_in_flight False, and run_stop_gate DOES call the
     evaluator (non-deferred evaluation happens; deferral is not permanent).
"""
from __future__ import annotations
import json, os, sys, time, threading, traceback
from pathlib import Path

REPO = Path.cwd()
for p in (REPO, REPO / "frontends"):
    sys.path.insert(0, str(p))

TAG = time.strftime("%Y%m%d-%H%M%S")
TARGET = Path("D:/test20260416") / f"_goal_gate_{TAG}"
DATA = REPO / "temp" / "goal_gate" / f"data_{TAG}"
LOGDIR = REPO / "temp" / "goal_gate"; LOGDIR.mkdir(parents=True, exist_ok=True)
LOG = (LOGDIR / f"gate_{TAG}.log").open("w", encoding="utf-8")
CHECKS = []
def log(*a):
    s = " ".join(str(x) for x in a); LOG.write(s + "\n"); LOG.flush(); print(s, flush=True)
def rec(name, ok, detail=""):
    CHECKS.append({"name": name, "ok": bool(ok), "detail": str(detail)})
    log(("PASS " if ok else "FAIL ") + name + (" - " + str(detail) if detail else ""))

os.environ.update({
    "GA_PROJECT_ROOT": str(TARGET), "GA_DATA_DIR": str(DATA),
    "GA_WORKFLOW_WORKSPACE_ROOT": str(TARGET), "GA_LAUNCH_CWD": str(TARGET),
    "GA_ORIGINAL_CWD": str(TARGET), "GA_LLM_CONFIG": str(REPO / "llm.yaml"),
})
for k in ("GA_PROJECT_KEY","GA_PROJECT_DATA_DIR","GA_SESSION_DATA_DIR",
          "GA_PARENT_SESSION_ID","GA_TASK_ROOT","GA_TEAM_KEY"):
    os.environ.pop(k, None)
os.environ.pop("GA_GOAL_WAKE", None)
TARGET.mkdir(parents=True, exist_ok=True)

from frontends.ink.bridge import GenericAgentBridge
from genericagent.core.handler import GenericAgentHandler
from genericagent.core import goal_evaluator as ge
from genericagent import paths as ga_paths

# marker paths use forward slashes so no backslash ever crosses a heredoc/JS layer
M_WF = TARGET.as_posix() + "/marker_wf.txt"
M_TEAM = TARGET.as_posix() + "/marker_team.txt"

def goal_status_events(path):
    out = []
    if not path or not Path(path).is_file(): return out
    for ln in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        ln = ln.strip()
        if not ln: continue
        try: o = json.loads(ln)
        except Exception: continue
        if isinstance(o, dict) and o.get("type") == "goal_status": out.append(o)
    return out

def main() -> int:
    summary = {"target": str(TARGET), "model": "default/deepseek-v4.1-flash"}
    rec_ev = []
    bridge = agent = None
    try:
        bridge = GenericAgentBridge(emit=lambda e: rec_ev.append(e)); agent = bridge.agent
        threading.Thread(target=agent.run, daemon=True).start()
        bridge.model_switch("default/deepseek-v4.1-flash"); time.sleep(2)

        goal_text = (f"目录 {TARGET} 下同时存在 marker_wf.txt(WF_OK) 与 marker_team.txt(TEAM_OK)")
        agent._set_goal(goal_text)
        rec("goal set and active", agent.active_goal is not None,
            getattr(agent.active_goal, "condition", None))

        # build the workflow script on disk (forward-slash paths; no backslash layers)
        wf_js = (
            "export const meta = { name: 'gate-wf', description: 'slow write marker_wf' }\n"
            "phase('write')\n"
            "const cmd = \"python -c \\\"import time;time.sleep(30);open(r'__MV__','w').write('WF_OK')\\\"\"\n"
            "const a = await agent(`Use code_run to run exactly this command, then stop: ${cmd}`, { label: 'slowwf' })\n"
            "return { summary: a.summary }\n"
        )
        # NOTE: the line above deliberately uses ${cmd} interpolation to avoid
        # embedding nested quotes directly; __MV__ is substituted to the path.
        wf_js = wf_js.replace("__MV__", M_WF)
        wf_path = TARGET / "wf_gate.js"
        wf_path.write_text(wf_js, encoding="utf-8", newline="\n")
        summary["wf_script"] = str(wf_path)

        # 1) launch a real background workflow (bridge daemon thread, real runtime)
        launch = bridge.workflow_launch_from_tool(script_path=str(wf_path))
        summary["wf_launch"] = launch
        rec("workflow launched async (async_launched)", launch.get("status") == "async_launched", launch.get("status"))
        wf_run_id = str(launch.get("runId") or "")
        summary["wf_run_id"] = wf_run_id

        # 2) spawn a real team_mode subagent (real process, real model) deterministically
        project_root = str(getattr(agent, "project_root", None) or ga_paths.PROJECT_ROOT)
        tool_cwd = str(getattr(agent, "tool_cwd", None) or project_root)
        h = GenericAgentHandler(agent, agent.history, tool_cwd, workspace_root=project_root)
        h.hook_runtime = getattr(agent, "hook_runtime", None)
        if h.hook_runtime is None:
            from genericagent.hooks.runtime import hook_runtime_for_agent
            h.hook_runtime = hook_runtime_for_agent(agent)
        mgr = h._get_subagent_manager()
        team_msg = (
            "Run exactly this ONE shell command with code_run, then reply done:\n"
            f"python -c \"import time;time.sleep(30);open(r'{M_TEAM}','w').write('TEAM_OK')\"\n"
            "Do not do anything else."
        )
        handle = mgr.spawn_agent(
            "teammate1", team_msg,
            parent_session_id=str(getattr(agent, "session_id", "")),
            background=True,
            capability_options={"team_mode": True},
        )
        summary["team_handle"] = str(getattr(handle, "agent_path", handle))
        rec("team_mode subagent spawned", bool(handle), summary["team_handle"])

        # 3) both channels must be in flight together, right now
        time.sleep(5)
        wf_labels = ge._workflow_runs_in_flight(agent)
        try:
            states = mgr.list_descendants(str(getattr(agent, "session_id", "")))
            sub_running = [s for s in states if ge.is_in_flight_agent_state(s)]
            sub_detail = [f"{getattr(s,'task_name','?')}:{getattr(s,'turn_status','?')}/{getattr(s,'process_status','?')}" for s in states]
        except Exception as exc:
            sub_running = []; sub_detail = [f"error:{exc}"]
        both = ge.has_in_flight_agents(agent, h)
        summary.update(wf_labels_inflight=wf_labels, sub_labels_inflight=sub_detail,
                       both_inflight=both)
        rec("workflow channel in flight", len(wf_labels) >= 1, wf_labels)
        rec("subagent (team) channel in flight", len(sub_running) >= 1, sub_detail)
        rec("has_in_flight_agents True while BOTH active", both is True, both)

        # 4) controlled Stop gate while BOTH are in flight: must defer, no eval
        runner_calls = []
        orig = h.hook_runtime.prompt_runner
        def wrapped(kind, prompt, payload):
            runner_calls.append(kind)
            return orig(kind, prompt, payload) if orig else "{}"
        h.hook_runtime.prompt_runner = wrapped
        before = len(runner_calls)
        gs_before = len(goal_status_events(getattr(agent, "session_path", None)))
        action, text, agg = h.run_stop_gate({})
        summary["gate_action"] = action
        summary["gate_last_goal_defer"] = getattr(h, "last_goal_defer", None)
        rec("Stop gate while BOTH in flight returns 'none'", action == "none", action)
        rec("Stop gate recorded last_goal_defer=True", getattr(h, "last_goal_defer", None) is True,
            getattr(h, "last_goal_defer", None))
        rec("goal evaluator model NOT called during defer", len(runner_calls) == before,
            f"{before}->{len(runner_calls)}")
        gs = goal_status_events(getattr(agent, "session_path", None))
        deferred = [g for g in gs if str(g.get("reason")) == "deferred"]
        rec("transcript recorded goal_status(reason='deferred')", len(deferred) >= 1,
            json.dumps(deferred[-1:], ensure_ascii=False)[:240])
        rec("goal still active after deferred gate", agent.active_goal is not None,
            getattr(agent.active_goal, "iterations", None))

        # 5) wait for both background jobs to settle
        wf_ok = team_ok = False
        end = time.time() + 300
        while time.time() < end:
            wf_ok = Path(M_WF).is_file() and "WF_OK" in Path(M_WF).read_text(encoding="utf-8", errors="replace")
            team_ok = Path(M_TEAM).is_file() and "TEAM_OK" in Path(M_TEAM).read_text(encoding="utf-8", errors="replace")
            # also require the workflow run terminal + subagent not running
            try:
                run = bridge.workflow_store.load_run(wf_run_id)
                wf_terminal = str(getattr(run, "status", "")) in {"succeeded","degraded","failed","cancelled","killed","interrupted"}
            except Exception:
                wf_terminal = False
            if wf_ok and team_ok and wf_terminal:
                break
            time.sleep(2)
        summary["wf_marker_ok"] = wf_ok
        summary["team_marker_ok"] = team_ok
        rec("workflow wrote marker_wf.txt (WF_OK)", wf_ok, M_WF)
        rec("team subagent wrote marker_team.txt (TEAM_OK)", team_ok, M_TEAM)
        time.sleep(8)  # let process status settle

        # 6) after both settle: no longer in flight, gate DOES evaluate (non-deferred)
        still = ge.has_in_flight_agents(agent, h)
        summary["inflight_after_settle"] = still
        rec("no in-flight background work after both settle", still is False, still)
        before2 = len(runner_calls)
        action2, text2, agg2 = h.run_stop_gate({})
        summary["gate2_action"] = action2
        summary["gate2_last_goal_defer"] = getattr(h, "last_goal_defer", None)
        rec("second Stop gate does NOT defer (last_goal_defer False)",
            getattr(h, "last_goal_defer", None) is False, getattr(h, "last_goal_defer", None))
        rec("goal evaluator model WAS called after settle (model called)",
            len(runner_calls) > before2, f"{before2}->{len(runner_calls)}")
        gs2 = goal_status_events(getattr(agent, "session_path", None))
        nondef = [g for g in gs2 if str(g.get("reason")) != "deferred"]
        summary["goal_status_tail"] = gs2[-4:]
        rec("a non-deferred goal_status exists after settle", len(nondef) >= 1,
            json.dumps(nondef[-1:], ensure_ascii=False)[:240])
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
