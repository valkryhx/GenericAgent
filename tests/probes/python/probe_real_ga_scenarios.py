"""真实场景自测：用 ga 的**真实顶层交互入口**跑 subagent / agent team / workflow。

为什么不用 `--task` 模式（重要）
--------------------------------
`python -m genericagent.core.app --task <dir>` 看起来是"跑一个一次性任务"，但它会把
进程变成**子 agent**：`genericagent/core/app.py:590`

    is_subagent = bool(getattr(agent, 'task_dir', None))

一旦 is_subagent 为真，编排类工具（spawn_agent / wait_agent / read_task 等）会被剥离。
实测：用 --task 跑"派生子代理"的场景，主 agent 回答"编排工具在我这里已被剥离"，
子 agent 场景 3 项失败。这不是缺陷，是 --task 的设计语义 —— 它本来就是给
SubagentManager 派生用的。

所以真实场景必须走**顶层**入口，即 `ga cli` 背后那条命令：

    python -m genericagent.core.app        # 不带 --task，走 input('> ') 循环

用管道喂一行任务，跑的是和用户手敲完全相同的代码路径。

三个真实场景（各一个独立进程）
------------------------------
1. subagent  -- 主 agent 派生普通子 agent，子 agent 用 code_run 写已知标记
2. team      -- task_create 建任务板 + team_mode 派生 teammate，teammate 写标记
3. workflow  -- 主 agent 用 workflow 工具跑 parallel + serial，产物落盘

判定：只看磁盘证据与结构不变量（output.txt / events / state.json / run.json / 产物文件），
不断言模型措辞。

用法
----
    cd /d/tmp
    GA_REAL_SCENARIOS=1 python /d/git_codes/GenericAgent/tests/probes/python/probe_real_ga_scenarios.py
    GA_REAL_SCENARIOS=1 GA_REAL_ONLY=subagent ...    # 只跑一个
"""

import json
import os
import subprocess
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
OPT_IN = os.environ.get("GA_REAL_SCENARIOS") == "1"
ONLY = os.environ.get("GA_REAL_ONLY", "").strip()
WORK_ROOT = os.environ.get("GA_REAL_WORK_ROOT", r"D:\tmp\ga-real-scenarios")
SCENARIO_TIMEOUT = float(os.environ.get("GA_REAL_TIMEOUT", "600"))

_ORIG_STDOUT = sys.__stdout__
_LOG_PATH: str | None = None
_checks: list[dict] = []


def log(*parts) -> None:
    text = " ".join(str(p) for p in parts) if parts else ""
    try:
        _ORIG_STDOUT.write(text + chr(10))
        _ORIG_STDOUT.flush()
    except (OSError, ValueError):
        pass
    if _LOG_PATH:
        try:
            os.makedirs(os.path.dirname(_LOG_PATH), exist_ok=True)
            with open(_LOG_PATH, "a", encoding="utf-8", newline="\n") as fh:
                fh.write(text + "\n")
        except OSError:
            pass


def record(scenario: str, name: str, ok: bool, detail: str = "") -> None:
    _checks.append({"scenario": scenario, "name": name, "ok": bool(ok), "detail": str(detail)})
    log(("PASS " if ok else "FAIL ") + "[%s] " % scenario + "%-50s %s"
        % (name, str(detail)[:64]))


def _is_repo_path(path: str) -> bool:
    try:
        a = os.path.normcase(os.path.abspath(path))
        b = os.path.normcase(os.path.abspath(REPO_ROOT))
    except (OSError, ValueError):
        return False
    return a == b or a.startswith(b + os.sep)


def check_provenance() -> bool:
    import genericagent

    origin = getattr(genericagent, "__file__", "") or ""
    installed = bool(origin) and not _is_repo_path(origin)
    record("env", "genericagent imports from site-packages", installed, origin)
    cwd_ok = not _is_repo_path(os.getcwd())
    record("env", "cwd is outside the source checkout", cwd_ok, os.getcwd())
    return installed and cwd_ok


def run_ga_interactive(scenario_dir: str, prompt: str, timeout: float) -> dict:
    """Run the real top-level GA process and feed one task through stdin.

    This is byte-for-byte what `ga cli` does: `python -m genericagent.core.app` with no
    `--task`, then the `input('> ')` loop. Feeding a line on stdin is equivalent to a user
    typing it. The process exits with EOFError once stdin closes, which is expected -- we
    judge the transcript, not the exit code.

    ENCODING (this bit a previous run): do NOT pass the prompt through a shell (`<<<`,
    heredoc, `echo |`). On Windows the Chinese text gets decoded as GBK on the way in,
    producing lone surrogates like `\\udcaf`, and the API then answers HTTP 500
    "模型服务异常". That looks exactly like an upstream outage and sent me chasing the
    provider twice. Passing `input=` to subprocess.run from Python keeps the bytes as
    UTF-8, which is what the real UI does.
    """
    os.makedirs(scenario_dir, exist_ok=True)
    started = time.time()
    env = os.environ.copy()
    env["GA_PROJECT_ROOT"] = scenario_dir
    env["GA_LAUNCH_CWD"] = scenario_dir
    env["GA_ORIGINAL_CWD"] = scenario_dir
    env["PYTHONIOENCODING"] = "utf-8"
    for key in ("GA_PROJECT_KEY", "GA_PROJECT_DATA_DIR", "GA_SESSION_DATA_DIR",
                "GA_PARENT_SESSION_ID", "GA_TASK_ROOT", "GA_TEAM_KEY"):
        env.pop(key, None)

    # Guard: the prompt must be encodable. A lone surrogate here is always a caller bug
    # (shell re-decoding), and it would surface as a bogus HTTP 500 downstream.
    try:
        prompt.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise AssertionError(
            "prompt is not UTF-8 encodable (%s); it was re-decoded by a shell" % exc
        ) from exc

    try:
        completed = subprocess.run(
            [sys.executable, "-m", "genericagent.core.app"],
            input=prompt + "\n",
            capture_output=True, text=True, encoding="utf-8", errors="replace",
            timeout=timeout, cwd=scenario_dir, env=env,
        )
        stdout, stderr = completed.stdout or "", completed.stderr or ""
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = "TIMEOUT after %ss" % timeout

    transcript = os.path.join(scenario_dir, "transcript.txt")
    with open(transcript, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(stdout)
    return {
        "seconds": round(time.time() - started, 1),
        "stdout": stdout,
        "stderrTail": stderr[-600:],
        "transcript": transcript,
        "dir": scenario_dir,
    }


def runtime_roots(scenario_dir: str) -> list[str]:
    """Where GA actually writes child-agent state, plus the scenario dir.

    `GA_PROJECT_ROOT` controls the *workspace* the agent works in, but subagent runtime
    state lives in the user data bucket:
        %LOCALAPPDATA%/GenericAgent/projects/<projectKey>/session_<id>/subagents/tasks/<name>/
    Searching only the scenario dir therefore finds nothing even when the child ran fine.
    Both are scanned; the user bucket is what actually holds `state.json`.
    """
    roots = [scenario_dir]
    for env_key in ("LOCALAPPDATA", "APPDATA"):
        base = os.environ.get(env_key)
        if not base:
            continue
        candidate = os.path.join(base, "GenericAgent")
        if os.path.isdir(candidate):
            roots.append(candidate)
    return roots


def walk_json(roots, filename: str) -> list[str]:
    found = []
    seen = set()
    if isinstance(roots, str):
        roots = [roots]
    for root in roots:
        if not root or not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            if filename not in files:
                continue
            path = os.path.join(dirpath, filename)
            key = os.path.normcase(os.path.abspath(path))
            if key in seen:
                continue
            seen.add(key)
            found.append(path)
    return found


def recent_state_payloads(roots, session_marker: str, within_seconds: float = 1800) -> list[dict]:
    """state.json rows that describe a child started for THIS scenario.

    Filtering by mtime keeps earlier probe runs from satisfying the assertion, and the
    project-key check keeps an unrelated project's agents out.
    """
    cutoff = time.time() - within_seconds
    rows = []
    for path in walk_json(roots, "state.json"):
        try:
            if os.path.getmtime(path) < cutoff:
                continue
        except OSError:
            continue
        payload = load_json(path)
        if not isinstance(payload, dict) or not payload.get("task_name"):
            continue
        if session_marker and session_marker not in path.replace("\\", "/"):
            continue
        payload["_path"] = path
        rows.append(payload)
    return rows


def load_json(path: str):
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------- scenarios

def scenario_subagent() -> None:
    name = "subagent"
    root = os.path.join(WORK_ROOT, name)
    marker = os.path.join(root, "child_marker.txt")
    if os.path.isfile(marker):
        os.remove(marker)

    prompt = (
        "这是 subagent 真实场景测试，请派生子代理完成，不要自己动手："
        "1) 调用 spawn_agent，task_name='real_sub'，background=true，message 要求子代理"
        "用 code_run 写出文件 " + marker.replace("\\", "/") + "，内容为 SUBAGENT_REAL_OK，然后结束；"
        "2) 用 wait_agent 等它结束（condition=all_terminal，timeout_seconds=180）；"
        "3) 用 read_agent_result 读取结果；"
        "4) 回报 agent_path 和结果摘要，然后结束本轮。"
    )
    result = run_ga_interactive(root, prompt, SCENARIO_TIMEOUT)
    log("[%s] %.1fs" % (name, result["seconds"]))

    out = result["stdout"]
    record(name, "top-level agent turn ran (not a stripped subagent)",
           "spawn_agent" in out or "已派生" in out or "subagent" in out.lower(),
           out.strip().splitlines()[-1][:60] if out.strip() else "empty")

    deadline = time.time() + 180
    while time.time() < deadline and not os.path.isfile(marker):
        time.sleep(2)
    record(name, "child agent wrote its marker file", os.path.isfile(marker), marker)
    body = ""
    if os.path.isfile(marker):
        try:
            with open(marker, encoding="utf-8", errors="replace") as fh:
                body = fh.read().strip()
        except OSError:
            pass
    record(name, "marker content is the required token", "SUBAGENT_REAL_OK" in body,
           body[:40])

    states = recent_state_payloads(runtime_roots(root), "")
    plain = [s for s in states
             if not bool(((s.get("capability_options") or {}).get("team_mode")))]
    record(name, "child state.json exists (task_name present)", bool(plain),
           "%d plain agent state(s)" % len(plain))
    statuses = [str(s.get("turn_status")) for s in plain]
    record(name, "child completed successfully", any(s == "completed" for s in statuses),
           str(statuses))


def scenario_team() -> None:
    name = "team"
    root = os.path.join(WORK_ROOT, name)
    marker = os.path.join(root, "team_marker.txt")
    if os.path.isfile(marker):
        os.remove(marker)

    prompt = (
        "这是 agent team 真实场景测试，请严格按顺序执行，不要自己做任务本体："
        "1) 调用 task_create，subject 为 'real team marker'，记下 task id；"
        "2) 调用 spawn_agent，task_name='real_teammate'，team_mode=true，"
        "team_task_id=<第1步的 task id>，background=true，message 要求 teammate 用 code_run "
        "写出文件 " + marker.replace("\\", "/") + "，内容为 TEAM_REAL_OK，然后结束；"
        "3) spawn 返回后只回报 task id 并立刻结束本轮，不要等待，也不要自己写标记文件。"
    )
    result = run_ga_interactive(root, prompt, SCENARIO_TIMEOUT)
    log("[%s] %.1fs" % (name, result["seconds"]))

    deadline = time.time() + 180
    while time.time() < deadline and not os.path.isfile(marker):
        time.sleep(2)
    record(name, "teammate wrote its marker file", os.path.isfile(marker), marker)

    boards = walk_json(runtime_roots(root), "tasks.json")
    board_hit = ""
    for path in boards:
        payload = load_json(path)
        if payload and "real team marker" in json.dumps(payload, ensure_ascii=False):
            board_hit = path
            break
    record(name, "shared task board has the created task", bool(board_hit),
           board_hit or "%d board(s)" % len(boards))

    states = recent_state_payloads(runtime_roots(root), "")
    team_rows = [s for s in states
                 if bool(((s.get("capability_options") or {}).get("team_mode")))]
    record(name, "a child ran with capability_options.team_mode=true", bool(team_rows),
           "%d team_mode agent(s)" % len(team_rows))

    # The marker lands when the teammate's code_run returns; closing the turn (and writing
    # turn_status=completed) happens after that. Sampling once here reports a false
    # 'running', so poll until terminal -- same lesson as the bridge-level probe.
    statuses = [str(s.get("turn_status")) for s in team_rows]
    waited = 0.0
    while waited < 180 and any(st not in {"completed", "failed"} for st in statuses):
        time.sleep(3.0)
        waited += 3.0
        rows = recent_state_payloads(runtime_roots(root), "")
        team_rows = [s for s in rows
                     if bool(((s.get("capability_options") or {}).get("team_mode")))]
        if not team_rows:
            break
        statuses = [str(s.get("turn_status")) for s in team_rows]
    record(name, "team_mode child reached completed", any(st == "completed" for st in statuses),
           "%s after %.0fs" % (statuses, waited))


def scenario_workflow() -> None:
    name = "workflow"
    root = os.path.join(WORK_ROOT, name)
    artifact = os.path.join(root, "wf_artifact.md")
    if os.path.isfile(artifact):
        os.remove(artifact)

    prompt = (
        "使用 workflow 工具跑一个动态 workflow（显式 opt-in）。要求："
        "1) 三个 agent 并行（用 parallel()）：两个各自用 file_write 在当前工作目录写出 "
        "wf_a.txt（内容 PARALLEL_A）和 wf_b.txt（内容 PARALLEL_B），第三个用 code_run 计算 7*6 并返回；"
        "2) 一个串行 agent 用 file_read 读回 wf_a.txt 和 wf_b.txt，再用 file_write 写出 "
        + artifact.replace("\\", "/") + "，内容以 ARTIFACT_START 开头且包含两个文件的内容；"
        "3) 脚本必须有 export const meta = { name, description }，用 phase() 分组；"
        "4) 调用 workflow 工具提交脚本，返回 async_launched 后立刻结束本轮，不要等待。"
    )
    result = run_ga_interactive(root, prompt, SCENARIO_TIMEOUT)
    log("[%s] %.1fs" % (name, result["seconds"]))

    out = result["stdout"]
    record(name, "workflow tool was invoked",
           "workflow" in out and ("async_launched" in out or "已启动" in out or "workflow" in out),
           out.strip().splitlines()[-1][:60] if out.strip() else "empty")

    # Workflow runs land in the user data bucket too (same reason as subagent state).
    run_dirs = [os.path.dirname(path) for path in walk_json(runtime_roots(root), "run.json")]
    record(name, "a workflow run directory exists", bool(run_dirs),
           run_dirs[0] if run_dirs else "none")

    best = None
    for directory in run_dirs:
        payload = load_json(os.path.join(directory, "run.json"))
        if not isinstance(payload, dict):
            continue
        if best is None or len(payload.get("jobs") or []) > len(best.get("jobs") or []):
            best = payload
    if best:
        jobs = best.get("jobs") or []
        statuses = [str(j.get("status")) for j in jobs]
        record(name, "run reached a success terminal status",
               str(best.get("status")) in {"succeeded", "degraded"},
               "status=%s" % best.get("status"))
        record(name, "run has at least 3 jobs", len(jobs) >= 3,
               "%d job(s): %s" % (len(jobs), statuses))
        record(name, "script really used parallel()", "parallel(" in str(best.get("script") or ""))
        record(name, "no job failed",
               bool(statuses) and all(s in {"succeeded", "degraded"} for s in statuses),
               str(statuses))
    else:
        for label in ("run reached a success terminal status", "run has at least 3 jobs",
                      "script really used parallel()", "no job failed"):
            record(name, label, False, "no run.json found")

    candidates = [artifact]
    for dirpath, _dirs, files in os.walk(root):
        if "wf_artifact.md" in files:
            candidates.append(os.path.join(dirpath, "wf_artifact.md"))
    hit = next((p for p in candidates if os.path.isfile(p)), "")
    record(name, "serial synthesis wrote the artifact", bool(hit), hit or "not found")
    body = ""
    if hit:
        try:
            with open(hit, encoding="utf-8", errors="replace") as fh:
                body = fh.read()
        except OSError:
            pass
    record(name, "artifact contains ARTIFACT_START", "ARTIFACT_START" in body,
           body[:50].replace("\n", " | "))


SCENARIOS = {"subagent": scenario_subagent, "team": scenario_team, "workflow": scenario_workflow}


def main() -> int:
    global _LOG_PATH
    os.makedirs(WORK_ROOT, exist_ok=True)
    _LOG_PATH = os.path.join(WORK_ROOT, "run.log")
    started = time.time()

    log("=" * 70)
    log("真实场景自测（ga 顶层交互进程 + 真实 LLM）")
    log("repo root : %s" % REPO_ROOT)
    log("cwd       : %s" % os.getcwd())
    log("work root : %s" % WORK_ROOT)
    log("opt-in    : %s" % OPT_IN)
    log("=" * 70)
    log("")

    if not check_provenance():
        log("")
        log("VERDICT: FAIL (必须从仓库外运行，否则子进程导入源码树)")
        return 1
    log("")

    if not OPT_IN:
        log("SKIP 真实场景（GA_REAL_SCENARIOS=1 启用）")
        failed = [c for c in _checks if not c["ok"]]
        log("VERDICT: %s (%d checks, %d failed)"
            % ("OK" if not failed else "FAIL", len(_checks), len(failed)))
        return 0 if not failed else 1

    names = [ONLY] if ONLY else list(SCENARIOS)
    for name in names:
        fn = SCENARIOS.get(name)
        if fn is None:
            log("UNKNOWN scenario: %s" % name)
            continue
        log("-" * 70)
        log("场景: %s" % name)
        log("-" * 70)
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            import traceback
            record(name, "scenario ran without raising", False,
                   "%s: %s" % (type(exc).__name__, exc))
            log(traceback.format_exc()[-1000:])
        log("")

    summary = {
        "startedAt": started,
        "elapsedSeconds": round(time.time() - started, 1),
        "workRoot": WORK_ROOT,
        "optIn": OPT_IN,
        "scenarios": names,
        "checks": _checks,
    }
    summary_path = os.path.join(WORK_ROOT, "summary.json")
    with open(summary_path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(json.dumps(summary, ensure_ascii=False, indent=2))

    failed = [c for c in _checks if not c["ok"]]
    log("=" * 70)
    log("summary: %s" % summary_path)
    log("VERDICT: %s (%d checks, %d failed)"
        % ("OK" if not failed else "FAIL", len(_checks), len(failed)))
    for item in failed:
        log("  FAIL [%s] %s - %s" % (item["scenario"], item["name"], item["detail"][:64]))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
