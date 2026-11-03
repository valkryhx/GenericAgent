"""Real-LLM regression: parallel workflow writers whose ``deliverables`` live
OUTSIDE the run workspace must not fail at startup.

Bug it guards (seen 2026-11-03 after e48dbae): a workflow whose agents write to
an absolute ``outDir`` outside the run workspace (default ``pathPolicy:
unrestricted``) had EVERY agent fail before the model ran, with
``path is outside workspace: <outDir>/...``. Root cause:
``_prepare_declared_workspace`` resolved advisory ``deliverables`` through the
workspace-only resolver and let ``WorkspacePathError`` fail the job.

This test asserts PROCESS evidence (per the real-LLM test strategy doc), not
just the final result:
  - the run succeeds;
  - each parallel job actually produced token usage > 0 and at least one
    assistant step / tool call (i.e. the model really ran -- the bug fails
    before the model, so token usage would be empty);
  - no job error mentions "outside workspace";
  - each report file exists under the OUTSIDE directory, is non-empty, and was
    written during this run (mtime >= start), not a stale leftover.

Run:  python tests/real_workflow_external_dir_deliverables_e2e.py
Exit: 0 = PASS, 2 = FAIL. Drives the real default LLM (deepseek-v4.1-flash).
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

from genericagent.core.redaction import redact_sensitive_text, sanitize
from genericagent.workflow.child_agent import NativeGPTChildAgentRunner
from genericagent.workflow.llm import binding_from_profile
from genericagent.workflow.models import WorkflowRun
from genericagent.workflow.runtime import WorkflowRuntime
from genericagent.workflow.scheduler import SchedulerConfig
from genericagent.workflow.store import WorkflowStore

PROFILE = os.environ.get("GA_WORKFLOW_LLM_PROFILE", "default")
EXPECTED_MODEL = os.environ.get("GA_REAL_API_EXPECTED_MODEL", "deepseek-v4.1-flash")

CHECKS: list[dict] = []
def rec(name: str, ok: bool, detail=""):
    CHECKS.append({"name": name, "ok": bool(ok), "detail": str(detail)})
    print(("PASS " if ok else "FAIL ") + name + (" - " + str(detail) if detail else ""), flush=True)

WORKFLOW_SCRIPT = r"""
export const meta = { name: 'external-dir-deliverables-e2e', description: 'parallel writers whose deliverables are OUTSIDE the run workspace' }
const outDir = args.outDir      // an absolute dir OUTSIDE workspacePath
const topics = ['alpha', 'beta', 'gamma']

phase('Write')
const results = await parallel(
  topics.map((t) => () => {
    const outPath = `${outDir}/report_${t}.md`
    return agent(
      `Write a short markdown report about topic "${t}". Use file_write to create the file at exactly this absolute path: ${outPath} . One heading plus two bullet points is enough. Do not write anywhere else.`,
      {
        label: `write-${t}`,
        phase: 'Write',
        deliverables: [outPath],
        schema: {
          type: 'object',
          properties: { outPath: { type: 'string' }, ok: { type: 'boolean' } },
          required: ['outPath', 'ok'],
          additionalProperties: false,
        },
        maxTurns: 12,
      }
    )
  })
)
return { wrote: results.filter(Boolean).map((r) => r && r.outPath) }
"""

def _job_token_usage(run, job) -> dict:
    return job.metadata.get("tokenUsage") or {}

def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="ga_real_ext_deliv_"))
    workspace = root / "run_workspace"          # the run workspace (child cwd)
    workspace.mkdir(parents=True)
    outside = root / "outside_reports"          # absolute dir OUTSIDE the run ws
    # NOTE: do NOT pre-create `outside`; the fix must allow the child to create it.

    store = WorkflowStore(root=root / "runtime")
    run = store.create_run(
        WorkflowRun(run_id="wf_real_ext_deliv", session_id="real_ext_deliv",
                    script=WORKFLOW_SCRIPT, status="running")
    )
    started = time.time()
    try:
        binding = binding_from_profile(PROFILE)
        rec("profile resolves to expected model", binding.model_id == EXPECTED_MODEL,
            f"{PROFILE}->{binding.model_id}")
        runner = NativeGPTChildAgentRunner(profile_name=PROFILE, enable_tools=True, max_turns=16)
        outcome = WorkflowRuntime(
            store=store, runner=runner,
            scheduler_config=SchedulerConfig(max_concurrent=3, max_total=6),
            timeout_seconds=420.0,
        ).run(run, args={"workspacePath": str(workspace), "outDir": str(outside)})
        loaded = store.load_run(run.run_id)
        jobs = loaded.jobs

        rec("run succeeded", loaded.status == "succeeded", loaded.status)
        rec("all jobs succeeded", all(j.status == "succeeded" for j in jobs),
            [(j.job_id, j.status) for j in jobs])
        rec("no job failed with 'outside workspace'",
            not any("outside workspace" in str(j.error or "") for j in jobs),
            [str(j.error) for j in jobs if j.error])

        # PROCESS evidence: the model really ran (the bug fails before the model).
        token_ok = all(int((_job_token_usage(loaded, j) or {}).get("output_tokens") or 0) > 0 for j in jobs)
        rec("each job consumed model tokens (>0 output)", token_ok,
            [(j.job_id, (_job_token_usage(loaded, j) or {}).get("output_tokens")) for j in jobs])
        # Tool evidence lives in job.metadata["toolSummary"]["allowedTools"]
        # (there is no "toolCalls" key). The file write is proof the writer tool
        # actually executed; assert a writer tool ran, not just any tool.
        def _allowed_tools(j):
            ts = j.metadata.get("toolSummary") or {}
            return list(ts.get("allowedTools") or [])
        rec("each job ran a writer tool", all(any(t in ("file_write", "code_patch", "code_run") for t in _allowed_tools(j)) for j in jobs),
            [(j.job_id, _allowed_tools(j)) for j in jobs])

        # RESULT evidence: files exist OUTSIDE the run workspace, this run only.
        files = sorted(outside.glob("report_*.md")) if outside.exists() else []
        rec("reports written to OUTSIDE dir", len(files) == 3, [p.name for p in files])
        nonempty = [p for p in files if p.stat().st_size > 0]
        rec("reports are non-empty", len(nonempty) == 3, [(p.name, p.stat().st_size) for p in files])
        fresh = [p for p in files if p.stat().st_mtime + 1 >= started]
        rec("reports written during THIS run (mtime>=start)", len(fresh) == 3,
            [(p.name, round(p.stat().st_mtime - started, 1)) for p in files])
        # Files must be outside workspace, not inside (they were genuinely external).
        rec("no report leaked inside run workspace", not list(workspace.glob('report_*.md')))

        summary = {
            "runStatus": loaded.status,
            "result": sanitize(outcome.result),
            "outsideDir": str(outside),
            "workspace": str(workspace),
            "elapsed_secs": round(time.time() - started, 1),
            "jobs": [{"jobId": j.job_id, "label": j.metadata.get("label"), "status": j.status,
                      "outputTokens": (_job_token_usage(loaded, j) or {}).get("output_tokens"),
                      "allowedTools": list((j.metadata.get("toolSummary") or {}).get("allowedTools") or []),
                      "error": j.error} for j in jobs],
            "files": [p.name for p in files],
            "checks": CHECKS,
        }
    except Exception as exc:
        summary = {"error": redact_sensitive_text(str(exc)), "checks": CHECKS}

    outdir = REPO / "temp" / "real_ext_deliv"; outdir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (outdir / f"summary_{stamp}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2),
                                                 encoding="utf-8", newline="\n")
    n = sum(1 for c in CHECKS if c["ok"])
    print(f"RESULT {n}/{len(CHECKS)} checks passed -> {outdir / ('summary_' + stamp + '.json')}", flush=True)
    return 0 if CHECKS and n == len(CHECKS) else 2

if __name__ == "__main__":
    raise SystemExit(main())
