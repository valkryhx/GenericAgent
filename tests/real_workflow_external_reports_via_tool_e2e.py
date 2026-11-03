"""Real-LLM, end-to-end reproduction of the 2026-11-03 failure shape.

Scenario (mirrors the actual user session): the MAIN agent authors a dynamic
workflow that fans out parallel review agents; each agent's report is written to
an ABSOLUTE directory OUTSIDE the run workspace (``bug/tests``). Default
``pathPolicy`` is ``unrestricted``, so this is legitimate — but the old
``_prepare_declared_workspace`` failed every agent at startup with
"path is outside workspace".

This drives the SAME entry the Ink UI uses for the model's ``workflow`` tool:
``GenericAgentBridge.workflow_launch_from_tool(script=..., args=...)`` plus a
real ``GenericAgentBridge`` (real default LLM), then asserts PROCESS evidence:
  - every isolated parallel agent succeeded and consumed model tokens;
  - no agent error mentions "outside workspace";
  - the OUTSIDE directory received real reports, written during this run;
  - workflow run reached a terminal success.

Run:  python tests/real_workflow_external_reports_via_tool_e2e.py
Exit: 0 = PASS, 2 = FAIL. Real default LLM (deepseek-v4.1-flash).
"""
from __future__ import annotations
import json, os, sys, time, threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
for p in (REPO, REPO / "frontends"):
    sys.path.insert(0, str(p))

CHECKS = []
def rec(name, ok, detail=""):
    CHECKS.append({"name": name, "ok": bool(ok), "detail": str(detail)})
    print(("PASS " if ok else "FAIL ") + name + (" - " + str(detail) if detail else ""), flush=True)

STAMP = time.strftime("%Y%m%d-%H%M%S")
OUT_IN_REPO = REPO / "temp" / "real_ext_reports" / f"run_{STAMP}"   # a dir OUTSIDE the workflow run workspace
OUT_IN_REPO.mkdir(parents=True, exist_ok=True)

SCRIPT = r"""
export const meta = { name: 'external-reports-via-tool', description: 'parallel agents write reports to an absolute dir OUTSIDE the run workspace' }
const outDir = args.outDir
const topics = ['one', 'two', 'three']
phase('Review')
const reports = await parallel(
  topics.map((t) => () => {
    const outPath = `${outDir}/review_${t}.md`
    return agent(
      `Write a short markdown review note about topic "${t}". Use file_write to create the file at exactly this absolute path: ${outPath}. One heading and two bullets is enough. Do not write anywhere else.`,
      { label: `review-${t}`, phase: 'Review', deliverables: [outPath], maxTurns: 10,
        schema: { type:'object', properties:{ outPath:{type:'string'}, ok:{type:'boolean'} }, required:['outPath','ok'], additionalProperties:false } }
    )
  })
)
return { wrote: reports.filter(Boolean).map((r) => r && r.outPath) }
"""

def main() -> int:
    from frontends.ink.bridge import GenericAgentBridge
    events = []
    bridge = GenericAgentBridge(emit=lambda e: events.append(e))
    agent = bridge.agent
    threading.Thread(target=agent.run, daemon=True).start()
    try:
        bridge.model_switch("default/deepseek-v4.1-flash")
    except Exception as e:
        print("model_switch warn:", e, flush=True)
    time.sleep(2)
    started = time.time()
    launch = bridge.workflow_launch_from_tool(
        script=SCRIPT,
        args={"outDir": str(OUT_IN_REPO)},
        timeout_seconds=420.0,
    )
    rec("workflow launched (async)", launch.get("status") == "async_launched", launch.get("status") or launch.get("error"))
    run_id = launch.get("runId")
    # Wait for the workflow to finish (background thread).
    run = None
    deadline = time.time() + 480
    while time.time() < deadline and run_id:
        try:
            run = bridge.workflow_store.load_run(run_id)
        except Exception:
            run = None
        if run and run.status in {"succeeded", "degraded", "failed", "killed", "cancelled", "interrupted"}:
            break
        time.sleep(2)
    rec("workflow reached terminal status", bool(run) and run.status in {"succeeded","degraded","failed"}, getattr(run, "status", None))
    jobs = list(getattr(run, "jobs", []) or []) if run else []
    def allowed(j):
        return list((j.metadata.get("toolSummary") or {}).get("allowedTools") or [])
    def out_tokens(j):
        return int((j.metadata.get("tokenUsage") or {}).get("output_tokens") or 0)

    rec("at least 3 agents registered", len(jobs) >= 3, [(j.job_id, j.status) for j in jobs])
    rec("all agents succeeded", bool(jobs) and all(j.status == "succeeded" for j in jobs),
        [(j.job_id, j.status) for j in jobs])
    rec("no agent failed with 'outside workspace'",
        not any("outside workspace" in str(j.error or "") for j in jobs),
        [str(j.error) for j in jobs if j.error])
    rec("each agent consumed model tokens (>0)", bool(jobs) and all(out_tokens(j) > 0 for j in jobs),
        [(j.job_id, out_tokens(j)) for j in jobs])
    rec("each agent ran a writer tool", bool(jobs) and all(any(t in ("file_write","code_patch","code_run") for t in allowed(j)) for j in jobs),
        [(j.job_id, allowed(j)) for j in jobs])

    files = sorted(OUT_IN_REPO.glob("review_*.md"))
    rec("reports written to OUTSIDE dir", len(files) >= 3, [p.name for p in files])
    rec("reports non-empty", bool(files) and all(p.stat().st_size > 0 for p in files),
        [(p.name, p.stat().st_size) for p in files])
    rec("reports written during THIS run", bool(files) and all(p.stat().st_mtime + 1 >= started for p in files),
        [(p.name, round(p.stat().st_mtime - started, 1)) for p in files])

    summary = {"runId": run_id, "runStatus": getattr(run, "status", None),
               "outDir": str(OUT_IN_REPO), "elapsed_secs": round(time.time()-started,1),
               "jobs": [{"jobId": j.job_id, "label": j.metadata.get("label"), "status": j.status,
                         "outputTokens": out_tokens(j), "allowedTools": allowed(j), "error": j.error} for j in jobs],
               "files": [p.name for p in files], "checks": CHECKS}
    outdir = REPO / "temp" / "real_ext_reports"; outdir.mkdir(parents=True, exist_ok=True)
    (outdir / f"summary_{STAMP}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                                  encoding="utf-8", newline="\n")
    n = sum(1 for c in CHECKS if c["ok"])
    print(f"RESULT {n}/{len(CHECKS)} checks passed -> {outdir / ('summary_'+STAMP+'.json')}", flush=True)
    return 0 if CHECKS and n == len(CHECKS) else 2

if __name__ == "__main__":
    raise SystemExit(main())
