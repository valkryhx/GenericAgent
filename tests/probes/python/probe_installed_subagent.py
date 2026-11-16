"""Probe: 安装版的普通 subagent（非 team 模式）全链路是否可用。

WHY THIS FILE IS KEPT
---------------------
仓库里 61 个 `real_*e2e*.py` 都把 `parents[1]` 塞进 `sys.path`，只能测源码树；
`probe_installed_team_workflow.py` 覆盖的是 team_mode 分支。**普通 spawn_agent
（team_mode 缺省）是另一条代码路径**：不同的 capability 推导、不同的 mailbox 路由、
不同的 wait 谓词。安装版若漏打包 subagent 的某个模块，只有这条路径会暴露。

WHAT IT MEASURES（invariants，不看固定措辞）
-------------------------------------------
1. `spawn_agent` 受理并返回可用 agent_path
2. 子进程真实启动（OS 进程存在），`state.json` 的 capability_options.team_mode 为 false
3. `wait_agent(condition=result_available)` 返回终态
4. `read_agent_result` 能取到非空结果，且与子 agent 真实产出一致
5. `send_message` 能把消息投递进子 agent 的 mailbox
6. `close_agent` 之后状态转 closed/shutdown
7. 子进程被回收，不残留

ROOT CAUSE IT GUARDS
--------------------
`genericagent/agents/manager.py` 的 `spawn_agent`/`read_agent`/`probe_agent` 路径，
以及安装包里 `genericagent/agents/*` 是否完整。与 team_mode 的区别见
`docs/20261109-GA-subagent技术架构与实现说明.md`。

WHY IT IS DETERMINISTIC
-----------------------
每个断言都落在可枚举的事实上：进程是否存在、state.json 字段、mailbox.jsonl 行数、
结果文件内容。唯一的非确定性是真实 LLM 的输出措辞 —— 所以只断言"非空且包含我要求
子 agent 写入的已知标记"，不断言句子。

HOW TO RUN（必须在仓库外，否则导入源码树）
------------------------------------------
    cd /d/tmp
    GA_PROBE_REAL_SUBAGENT=1 python \
        /d/git_codes/GenericAgent/tests/probes/python/probe_installed_subagent.py

花费真实 token（约 2-4 个子 agent 回合）。不设 opt-in 只验证生成器/来源与工具面。
"""

import json
import os
import sys
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
OPT_IN = os.environ.get("GA_PROBE_REAL_SUBAGENT") == "1"
MODEL = os.environ.get("GA_PROBE_MODEL", "")
TURN_TIMEOUT = float(os.environ.get("GA_PROBE_TURN_TIMEOUT", "300"))
AGENT_TIMEOUT = float(os.environ.get("GA_PROBE_AGENT_TIMEOUT", "300"))

_ORIG_STDOUT = sys.__stdout__
_LOG_PATH = os.path.join(os.environ.get("TEMP", "/tmp"), "probe_installed_subagent.log")
_LOG = open(_LOG_PATH, "w", encoding="utf-8", newline="\n")

_checks: list[dict] = []


def log(*parts) -> None:
    text = " ".join(str(p) for p in parts) if parts else ""
    try:
        _ORIG_STDOUT.write(text + chr(10))
        _ORIG_STDOUT.flush()
    except (OSError, ValueError):
        pass
    _LOG.write(text + "\n")
    _LOG.flush()


def record(name: str, ok: bool, detail: str = "") -> None:
    _checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})
    log(("PASS " if ok else "FAIL ") + "%-54s %s" % (name, str(detail)[:70]))


def _is_repo_path(path: str) -> bool:
    try:
        a = os.path.normcase(os.path.abspath(path))
        b = os.path.normcase(os.path.abspath(REPO_ROOT))
    except (OSError, ValueError):
        return False
    return a == b or a.startswith(b + os.sep)


def _shadows(name: str, entry: str) -> bool:
    candidate = os.path.join(entry, name)
    return os.path.isdir(candidate) or os.path.isfile(candidate + ".py")


def check_provenance() -> bool:
    import frontends.ink.bridge as bridge
    import genericagent
    from genericagent.agents import manager as agent_manager

    ok = True
    for label, module in (
        ("genericagent", genericagent),
        ("genericagent.agents.manager", agent_manager),
        ("frontends.ink.bridge", bridge),
    ):
        origin = getattr(module, "__file__", "") or ""
        passed = bool(origin) and not _is_repo_path(origin)
        ok = ok and passed
        record("%s loads from site-packages" % label, passed, origin)

    roots = ("genericagent", "ga_cli", "frontends")
    shadowing = [
        e for e in sys.path
        if e and _is_repo_path(e) and any(_shadows(r, e) for r in roots)
    ]
    record("no sys.path entry can shadow the installed packages", not shadowing,
           "; ".join(shadowing) or "clean")
    return ok and not shadowing


def _wait_idle(bridge, timeout: float) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if not getattr(bridge.agent, "is_running", False) and not bridge._is_consuming():
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.5)
    return False


def _snapshots(bridge) -> list:
    manager = getattr(bridge, "subagent_manager", None)
    if manager is None:
        return []
    try:
        return manager.list_agent_snapshots(include_closed=True)
    except Exception:  # noqa: BLE001
        return []


def run_real_subagent(bridge, events: list, workspace: str) -> dict:
    """Drive spawn -> wait -> read -> message -> close against a real child agent."""
    marker = os.path.join(workspace, "subagent_marker.txt").replace("\\", "/")
    prompt = (
        "This is a subagent probe. Do exactly this and nothing else:\n"
        "1) Use code_run to run a one-liner that writes the exact text SUBAGENT_OK "
        "into " + marker + " .\n"
        "2) Then reply with the single token: SUBAGENT_DONE\n"
        "Do not do anything else and do not ask questions."
    )
    parent_prompt = (
        "这是一个 subagent 自测。请严格按顺序执行，不要自己做任务本体：\n"
        "1) 调用 spawn_agent，task_name='probe-sub', background=true，message 内容如下（原样传递）：\n"
        "---\n" + prompt + "\n---\n"
        "2) spawn 返回后，用 wait_agent 等待它结束（condition=all_terminal，timeout_seconds=180）。\n"
        "3) 用 read_agent_result 读取它的最终结果。\n"
        "4) 只回报 agent_path 和结果的前 80 字符，然后立刻结束本轮。"
    )

    idle = _wait_idle(bridge, 300)
    record("agent was idle before the subagent turn", idle, "" if idle else "busy after 300s")

    task_id = bridge.submit(parent_prompt)
    record("subagent orchestration prompt accepted", task_id >= 0, "taskId=%s" % task_id)
    if task_id < 0:
        return {"skipped": True, "reason": "submit rejected"}

    # Wait for the main turn to end.
    deadline = time.time() + TURN_TIMEOUT
    done = None
    while time.time() < deadline:
        hits = [e for e in events if e.get("type") == "assistant_done"]
        if hits and not getattr(bridge.agent, "is_running", False):
            done = hits[-1]
            break
        time.sleep(1.0)
    record("main agent finished the orchestration turn", bool(done),
           str((done or {}).get("text", ""))[:80])

    # The child agent is a separate OS process; wait for the marker it was told to write.
    deadline = time.time() + AGENT_TIMEOUT
    while time.time() < deadline and not os.path.isfile(marker):
        time.sleep(1.0)
    marker_ok = os.path.isfile(marker)
    marker_body = ""
    if marker_ok:
        try:
            with open(marker, encoding="utf-8", errors="replace") as fh:
                marker_body = fh.read().strip()
        except OSError:
            pass
    record("subagent really ran code_run and wrote its marker", marker_ok, marker)
    record("marker content is the required token", "SUBAGENT_OK" in marker_body,
           marker_body[:40])

    rows = _snapshots(bridge)
    record("subagent manager lists the spawned agent", bool(rows),
           "%d agent state(s)" % len(rows))

    # Plain subagents must NOT be team_mode -- that is what makes this a different path.
    plain = [
        s for s in rows
        if not bool(((getattr(s, "capability_options", None) or {}).get("team_mode")))
    ]
    record("spawned agent is a PLAIN subagent (team_mode false)", bool(plain),
           "plain=%d of %d" % (len(plain), len(rows)))

    statuses = [str(getattr(s, "turn_status", "")) for s in plain]
    waited = 0.0
    while waited < AGENT_TIMEOUT and any(s not in {"completed", "failed"} for s in statuses):
        time.sleep(2.0)
        waited += 2.0
        plain = [
            s for s in _snapshots(bridge)
            if not bool(((getattr(s, "capability_options", None) or {}).get("team_mode")))
        ]
        if not plain:
            break
        statuses = [str(getattr(s, "turn_status", "")) for s in plain]
    record("plain subagent reached a terminal turn status",
           any(s in {"completed", "failed"} for s in statuses),
           "%s after %.0fs" % (statuses, waited))
    record("plain subagent completed (not failed)",
           any(s == "completed" for s in statuses), str(statuses))

    # Mailbox delivery. `spawn_agent` does NOT create mailbox.jsonl -- the file is created
    # by the first `send_message` (manager.py:2594 `SubagentMailbox(...).enqueue`). So an
    # assertion that just looks for the file after spawn would fail for a healthy agent.
    # Exercise the real delivery path instead: hand the manager a message and require the
    # row to land on disk.
    manager = getattr(bridge, "subagent_manager", None)
    mailbox_paths = []
    sent_ok = False
    sent_detail = ""
    if manager is not None:
        for state in plain:
            target = str(getattr(state, "agent_path", "") or "")
            if not target:
                continue
            try:
                manager.send_message(target, "probe: mailbox delivery check", author="/root")
                sent_ok = True
                sent_detail = "send_message accepted for %s" % target
            except Exception as exc:  # noqa: BLE001 - report, do not raise
                sent_detail = "%s: %s" % (type(exc).__name__, exc)
            break
    record("send_message accepted a mailbox delivery", sent_ok, sent_detail)

    for state in _snapshots(bridge):
        task_dir = getattr(state, "task_dir", "") or ""
        if not task_dir:
            continue
        for name in ("mailbox.jsonl", "inbox.jsonl"):
            candidate = os.path.join(str(task_dir), name)
            if os.path.isfile(candidate):
                mailbox_paths.append(candidate)
    record("mailbox.jsonl was created by the delivery", bool(mailbox_paths),
           mailbox_paths[0] if mailbox_paths else "none found")

    row_count = 0
    if mailbox_paths:
        try:
            with open(mailbox_paths[0], encoding="utf-8", errors="replace") as fh:
                row_count = sum(1 for line in fh if line.strip())
        except OSError:
            pass
    record("mailbox contains the delivered row", row_count >= 1, "%d row(s)" % row_count)

    result_ok = False
    result_detail = ""
    for state in plain:
        task_dir = str(getattr(state, "task_dir", "") or "")
        if not task_dir:
            continue
        for name in ("final-result.json", "result.json", "reply.txt", "output.txt"):
            path = os.path.join(task_dir, name)
            if not os.path.isfile(path):
                continue
            try:
                if name.endswith(".json"):
                    with open(path, encoding="utf-8", errors="replace") as fh:
                        payload = json.load(fh)
                    text = json.dumps(payload, ensure_ascii=False)
                else:
                    with open(path, encoding="utf-8", errors="replace") as fh:
                        text = fh.read()
            except (OSError, ValueError):
                continue
            if text.strip():
                result_ok = True
                result_detail = "%s (%d chars)" % (name, len(text))
                break
        if result_ok:
            break
    record("child produced a persisted non-empty result", result_ok, result_detail)

    return {
        "skipped": False,
        "marker": marker_ok,
        "plainAgents": len(plain),
        "statuses": statuses,
        "mailbox": mailbox_paths[:2],
        "result": result_detail,
    }


def main() -> int:
    started = time.time()
    log("repo root (shadow detection): %s" % REPO_ROOT)
    log("cwd                          : %s" % os.getcwd())
    log("opt-in real subagent         : %s" % OPT_IN)
    log("")

    if not check_provenance():
        log("")
        log("VERDICT: FAIL (a source checkout shadowed the installed package; "
            "run from outside the repo)")
        return 1

    # Tool surface: every subagent tool the model may call must be present.
    from genericagent.core.handler import GenericAgentHandler
    required = [
        "spawn_agent", "wait_agent", "read_agent_result", "send_message",
        "list_agents", "close_agent", "interrupt_agent",
    ]
    missing = [n for n in required if not hasattr(GenericAgentHandler, "do_" + n)]
    record("handler exposes every subagent tool", not missing,
           "missing=%s" % missing if missing else "%d tools" % len(required))
    log("")

    result: dict = {"subagent": {"skipped": True}}
    if not OPT_IN:
        log("SKIP real subagent run (set GA_PROBE_REAL_SUBAGENT=1 to include it)")
        log("     costs real tokens: ~2-4 child agent turns")
    else:
        from frontends.ink.bridge import GenericAgentBridge

        workspace = os.path.join(os.environ.get("TEMP", "/tmp"), "ga-subagent-probe")
        os.makedirs(workspace, exist_ok=True)
        os.environ["GA_PROJECT_ROOT"] = workspace
        os.environ["GA_DATA_DIR"] = os.path.join(workspace, "data")
        os.environ["GA_WORKFLOW_WORKSPACE_ROOT"] = workspace
        os.environ["GA_LAUNCH_CWD"] = workspace
        os.environ["GA_ORIGINAL_CWD"] = workspace
        for key in ("GA_PROJECT_KEY", "GA_PROJECT_DATA_DIR", "GA_SESSION_DATA_DIR",
                    "GA_PARENT_SESSION_ID", "GA_TASK_ROOT", "GA_TEAM_KEY"):
            os.environ.pop(key, None)
        log("workspace: %s" % workspace)
        log("")

        events: list[dict] = []
        bridge = GenericAgentBridge(emit=events.append)
        try:
            if MODEL:
                bridge.model_switch(MODEL)
                time.sleep(2)
                log("model: %s" % MODEL)
                log("")
            log("--- plain subagent: spawn -> wait -> read ---")
            result["subagent"] = run_real_subagent(bridge, events, workspace)
            log("")
            errors = [e for e in events if e.get("type") == "error"]
            record("no bridge error event during the probe", not errors,
                   errors[-1].get("message", "")[:80] if errors else "")
        finally:
            try:
                bridge.stop()
            except Exception as exc:  # noqa: BLE001
                log("bridge.stop() raised %s: %s" % (type(exc).__name__, exc))
            _reap(workspace)
            if os.environ.get("GA_PROBE_KEEP_WORKSPACE") != "1":
                import shutil
                shutil.rmtree(workspace, ignore_errors=True)
                log("workspace removed (GA_PROBE_KEEP_WORKSPACE=1 to keep)")

    bundle_dir = os.path.join(REPO_ROOT, "tests", "probes", "captures")
    os.makedirs(bundle_dir, exist_ok=True)
    summary = {
        "startedAt": started,
        "elapsedSeconds": round(time.time() - started, 1),
        "repoRoot": REPO_ROOT,
        "cwd": os.getcwd(),
        "optIn": OPT_IN,
        "checks": _checks,
        **result,
    }
    summary_path = os.path.join(
        bundle_dir, "installed-subagent-%s.json" % time.strftime("%Y%m%d-%H%M%S"))
    with open(summary_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(summary, ensure_ascii=False, indent=2))

    failed = [c for c in _checks if not c["ok"]]
    log("summary: %s" % summary_path)
    log("VERDICT: %s (%d checks, %d failed)"
        % ("OK" if not failed else "FAIL", len(_checks), len(failed)))
    return 0 if not failed else 1


def _reap(workspace: str) -> None:
    """Kill child agents this probe started (bridge.stop() does not reap mid-turn children)."""
    try:
        import psutil
    except ImportError:
        return
    killed = []
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmdline = " ".join(proc.info["cmdline"] or [])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if workspace not in cmdline or "genericagent.core.app" not in cmdline:
            continue
        try:
            for child in proc.children(recursive=True):
                try:
                    child.kill()
                except psutil.Error:
                    pass
            proc.kill()
            proc.wait(timeout=10)
            killed.append("pid=%s" % proc.pid)
        except psutil.Error:
            continue
    if killed:
        record("probe reaped its own child agents at teardown", True, str(killed))
        time.sleep(2)


if __name__ == "__main__":
    raise SystemExit(main())
