"""Real-LLM regression: a single ``/goal`` writes an 8-queens HTML.

This is the exact user scenario that exposed the "0 turns / <1s / no file" bug:
``/goal <dir> 写一个8皇后一个解的html展示``. It proves the kickoff turn both
runs the model AND lands the artifact, and that the produced board is a legal
8-queens solution.

Run:  python tests/real_goal_slash_8queens_e2e.py
Exit: 0 = PASS, 1 = FAIL. Drives the real default LLM (deepseek-v4.1-flash).
"""
from __future__ import annotations
import json, re, sys, time, threading, traceback
from pathlib import Path

REPO = Path.cwd()
for p in (REPO, REPO / "frontends"):
    sys.path.insert(0, str(p))

TAG = time.strftime("%Y%m%d-%H%M%S")
TARGET = (REPO / "temp" / "goal_8queens" / f"run_{TAG}")
OUTDIR = REPO / "temp" / "goal_8queens"; OUTDIR.mkdir(parents=True, exist_ok=True)
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

N = 8
def is_valid_solution(cols):
    """cols[r] = column of the queen in row r; verify rows/cols/diagonals."""
    if len(cols) != N or any(c is None or c < 0 or c >= N for c in cols):
        return False
    if len(set(cols)) != N:
        return False
    for i in range(N):
        for j in range(i + 1, N):
            if abs(cols[i] - cols[j]) == j - i:
                return False
    return True

def parse_solution(html):
    """Pull the first int array of length 8 out of the generated HTML."""
    for m in re.finditer(r"\[\s*((?:\d+\s*,\s*){7}\d+)\s*\]", html):
        cols = [int(x) for x in m.group(1).split(",")]
        if len(cols) == 8:
            return cols
    return None

def main() -> int:
    TARGET.mkdir(parents=True, exist_ok=True)
    from frontends.ink.bridge import GenericAgentBridge
    from genericagent import paths
    ge = Path(paths.data_root()) / "logs" / "goal_events.jsonl"
    before = sum(1 for _ in ge.open(encoding="utf-8")) if ge.is_file() else 0

    events = []
    bridge = GenericAgentBridge(emit=lambda e: events.append(e))
    agent = bridge.agent
    threading.Thread(target=agent.run, daemon=True).start()
    try:
        bridge.model_switch("default/deepseek-v4.1-flash")
    except Exception as e:
        print("model_switch warn:", e)
    time.sleep(2)

    cond = (f"在目录 {TARGET} 里写一个 8 皇后问题的一个解的可视化 HTML 展示（单个 html 文件，"
            f"文件名 eight_queens.html），用 8x8 棋盘加样式画出皇后位置，并确认文件已生成")
    t0 = time.time()
    dq = agent.put_task("/goal " + cond, source="user")
    done = None; notes = []
    while time.time() - t0 < 600:
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

    produced = sorted(TARGET.glob("*.html"))
    html = produced[0].read_text(encoding="utf-8", errors="replace") if produced else ""
    cols = parse_solution(html)
    evs = []
    if ge.is_file():
        for line in ge.read_text(encoding="utf-8", errors="replace").splitlines():
            try: evs.append(json.loads(line))
            except Exception: pass
    new_events = [e.get("event") for e in evs[before:]]

    rec("single /goal ran a real model turn (>2s)", secs > 2, f"{secs}s")
    rec("assistant produced a final message", bool((done or "").strip()), f"len={len(done or '')}")
    rec("an .html file was produced", bool(produced), [p.name for p in produced])
    rec("html looks like a board page", bool(re.search(r"(?i)8\s*[x×]\s*8|board|chess", html)),
        len(html))
    rec("solution is a legal 8-queens placement", cols is not None and is_valid_solution(cols), cols)
    rec("goal reached met", "goal_met" in new_events, new_events)
    rec("goal auto-cleared after met (active_goal None)", getattr(agent, "active_goal", None) is None)

    summary = {"target": str(TARGET), "elapsed_secs": secs, "model": "default/deepseek-v4.1-flash",
               "solution": cols, "html_bytes": len(html), "goal_events_new": new_events, "checks": CHECKS}
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
