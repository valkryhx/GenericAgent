"""Real-LLM: main agent launched from the repo can read/write a user-specified
ABSOLUTE path OUTSIDE the repo (project A -> external dir B)."""
from __future__ import annotations
import json, os, sys, threading, time
from pathlib import Path

REPO = Path(r"D:/git_codes/GenericAgent")
for _p in (REPO, REPO / "frontends"):
    sys.path.insert(0, str(_p))

STAMP = time.strftime("%Y%m%d-%H%M%S")
OUT = Path("D:/test_ga_paths_verify3") / STAMP
OUT.mkdir(parents=True, exist_ok=True)
SEED = OUT / "seed.txt"
TOKEN = "SEED_TOKEN_" + STAMP.replace("-", "")
SEED.write_text("外部预置文件。TOKEN=" + TOKEN + "\n", encoding="utf-8")

for _k in ("GA_PROJECT_ROOT", "GA_WORKFLOW_WORKSPACE_ROOT", "GA_PROJECT_KEY",
           "GA_PROJECT_DATA_DIR", "GA_SESSION_DATA_DIR", "GA_TASK_ROOT", "GA_TEAM_KEY"):
    os.environ.pop(_k, None)
DATA = REPO / "temp" / "verify_outside3" / f"data_{STAMP}"
os.environ.update({
    "GA_DATA_DIR": str(DATA),
    "GA_LAUNCH_CWD": str(REPO),
    "GA_ORIGINAL_CWD": str(REPO),
    "GA_LLM_CONFIG": str(REPO / "llm.yaml"),
})

CHECKS = []
def rec(name, ok, detail=""):
    CHECKS.append({"name": name, "ok": bool(ok), "detail": str(detail)})
    print(("PASS " if ok else "FAIL ") + name + (" - " + str(detail) if detail else ""), flush=True)

from frontends.ink.bridge import GenericAgentBridge  # noqa: E402

events = []
_lock = threading.Lock()
def emit(e):
    with _lock:
        events.append(e)

def drain(task_id, timeout=300):
    t0 = time.time(); done = None
    while time.time() - t0 < timeout:
        with _lock:
            ds = [e for e in events if e.get("type") == "assistant_done" and e.get("taskId") == task_id]
            idle = [e for e in events if e.get("type") == "status" and e.get("status") == "idle" and e.get("taskId") == task_id]
        if ds: done = ds[-1].get("text", "")
        if idle: break
        time.sleep(0.4)
    return done, round(time.time() - t0, 1)

def stream_text_since(start):
    """The bridge does not emit tool calls as discrete events for the main
    turn; tool use shows up in the assistant stream text (``Tool: file_read``
    markers) and in assistant_done. Concatenate that text for assertions."""
    with _lock:
        chunks = [str(e.get("text") or "") for e in events[start:]
                  if e.get("type") in ("assistant_delta", "assistant_done")]
    return "\n".join(chunks)

def main() -> int:
    bridge = GenericAgentBridge(emit=emit)
    agent = bridge.agent
    threading.Thread(target=agent.run, daemon=True).start()
    try:
        bridge.model_switch("default/moma/deepseek-latest-flash")
    except Exception as e:
        print("model_switch warn:", e, flush=True)
    time.sleep(2)
    rec("IDENTITY: project_root is the repo (project A)", Path(agent.project_root).resolve() == REPO.resolve(), agent.project_root)
    rec("IDENTITY: external target is outside project_root", not str(OUT).lower().startswith(str(REPO).lower()), str(OUT))

    # (1) main agent: READ an existing external file + WRITE a nested external file
    nested = OUT / "a" / "b" / "hello.txt"
    p1 = (f"全部使用绝对路径，只做这些：\n"
          f"1) 用 file_read 读取外部文件 {SEED.as_posix()} ，取出 TOKEN 值。\n"
          f"2) 用 file_write 在 {nested.as_posix()} 新建文件（需要时自动创建 a/b 子目录），"
          f"第一行写 HELLO_EXT，第二行写读到的 TOKEN。\n"
          f"不要碰其他文件，完成后简短说明。")
    start = len(events)
    tid = bridge.submit(p1)
    done, secs = drain(tid, 300)
    stream = stream_text_since(start)
    txt = nested.read_text(encoding="utf-8", errors="replace") if nested.is_file() else ""
    rec("PLAIN: turn produced assistant text", bool((done or "").strip()), f"secs={secs}")
    rec("PLAIN: nested external file created", nested.is_file(), str(nested))
    rec("PLAIN: content HELLO_EXT", "HELLO_EXT" in txt, txt[:50].replace(chr(10), "|"))
    rec("PLAIN: READ external seed (token present)", TOKEN in txt, f"token={'yes' if TOKEN in txt else 'no'}")
    used_tool = ("file_read" in stream) or ("file_write" in stream)
    rec("PLAIN: actually used file_read/file_write", used_tool, "file_read" if "file_read" in stream else ("file_write" if "file_write" in stream else "none"))
    with _lock:
        denies = [e for e in events[start:] if "PathACL" in str(e)]
    rec("PLAIN: no PathACL deny in stream", not denies, len(denies))

    # (2) main agent: bash code_run must still run (previously forced Python-only)
    bash_dir = OUT / "bash_out"
    bash_dir.mkdir(parents=True, exist_ok=True)
    p2 = (f"用 code_run（type=bash）在外部目录 {bash_dir.as_posix()} 下执行一条命令："
          f"创建文件 marker.txt 并写入 BASH_OK。然后简短回复。绝对路径执行，不要用 file_write。")
    start = len(events)
    tid = bridge.submit(p2)
    done2, secs2 = drain(tid, 240)
    marker = bash_dir / "marker.txt"
    mtext = marker.read_text(encoding="utf-8", errors="replace") if marker.is_file() else ""
    rec("BASH: bash code_run created external file", marker.is_file(), str(marker))
    rec("BASH: marker content BASH_OK", "BASH_OK" in mtext, mtext[:40])
    rec("BASH: no 'only permits Python' error", "only permits Python" not in (done2 or ""), (done2 or "")[-160:])

    # (3) workflow child honors an ABSOLUTE deliverable outside the run workspace
    SCRIPT = r"""
export const meta = { name: 'outside-abs-target', description: 'child writes to a user absolute dir' }
const dir = args.outDir
phase('Do')
const [a, b] = await parallel([
  () => agent(`Use file_write to create the absolute file ${dir}/wf/alpha.md with first line WF_ALPHA_OK and second line 甲. Do nothing else.`,
    { label:'wf-alpha', phase:'Do', deliverables:[`${dir}/wf/alpha.md`], maxTurns:10,
      schema:{type:'object',properties:{outPath:{type:'string'}},required:['outPath'],additionalProperties:false}}),
  () => agent(`Use file_read to read the absolute file ${dir}/seed.txt , extract the TOKEN value, then file_write the absolute file ${dir}/wf/beta.md with first line WF_BETA_OK and second line the TOKEN. Do nothing else.`,
    { label:'wf-beta', phase:'Do', deliverables:[`${dir}/wf/beta.md`], maxTurns:10,
      schema:{type:'object',properties:{token:{type:'string'},outPath:{type:'string'}},required:['token','outPath'],additionalProperties:false}}),
])
return { a, b }
"""
    launch = bridge.workflow_launch_from_tool(script=SCRIPT, args={"outDir": OUT.as_posix()}, timeout_seconds=300.0)
    rec("WF: launched async", launch.get("status") == "async_launched", launch.get("status") or launch.get("error"))
    rid = launch.get("runId"); run = None; dl = time.time() + 360
    while time.time() < dl and rid:
        try: run = bridge.workflow_store.load_run(rid)
        except Exception: run = None
        if run and run.status in {"succeeded","degraded","failed","killed","cancelled","interrupted"}: break
        time.sleep(2)
    jobs = list(getattr(run, "jobs", []) or []) if run else []
    def allowed(j): return list((j.metadata.get("toolSummary") or {}).get("allowedTools") or [])
    def otok(j): return int((j.metadata.get("tokenUsage") or {}).get("output_tokens") or 0)
    rec("WF: run terminal", bool(run) and run.status in {"succeeded","degraded"}, getattr(run, "status", None))
    rec("WF: all jobs succeeded", bool(jobs) and all(j.status == "succeeded" for j in jobs), [(j.job_id, j.status) for j in jobs])
    rec("WF: each job consumed tokens", bool(jobs) and all(otok(j) > 0 for j in jobs), [(j.job_id, otok(j)) for j in jobs])
    alpha = OUT / "wf" / "alpha.md"; beta = OUT / "wf" / "beta.md"
    at = alpha.read_text(encoding="utf-8", errors="replace") if alpha.is_file() else ""
    bt = beta.read_text(encoding="utf-8", errors="replace") if beta.is_file() else ""
    rec("WF: alpha.md written to ABSOLUTE external dir", alpha.is_file() and "WF_ALPHA_OK" in at, str(alpha))
    rec("WF: beta.md written to ABSOLUTE external dir", beta.is_file() and "WF_BETA_OK" in bt, str(beta))
    rec("WF: beta READ external seed token", TOKEN in bt, f"token={'yes' if TOKEN in bt else 'no'}")

    summary = {
        "outsideDir": str(OUT), "seed": str(SEED), "token": TOKEN,
        "projectRoot": agent.project_root, "wfRun": rid,
        "plainSecs": secs, "bashSecs": secs2,
        "jobs": [{"jobId": j.job_id, "label": j.metadata.get("label"), "status": j.status,
                  "outputTokens": otok(j), "allowedTools": allowed(j), "error": j.error} for j in jobs],
        "files": sorted(str(p.relative_to(OUT)).replace(os.sep, "/") for p in OUT.rglob("*") if p.is_file()),
        "checks": CHECKS,
    }
    bundle = REPO / "temp" / "verify_outside3"
    bundle.mkdir(parents=True, exist_ok=True)
    (bundle / f"summary_{STAMP}.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    passed = sum(1 for c in CHECKS if c["ok"])
    print(f"RESULT {passed}/{len(CHECKS)} checks passed; outsideDir={OUT}", flush=True)
    try:
        agent.stop_sig = True
        agent.abort()
        bridge.stop()
    except Exception:
        pass
    return 0 if passed == len(CHECKS) else 1


if __name__ == "__main__":
    raise SystemExit(main())
