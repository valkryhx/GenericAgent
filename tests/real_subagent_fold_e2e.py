"""Real-API E2E: a subagent result must reach the main agent *mid-turn*.

The fold-point injection has unit coverage with a fake client, which is exactly the part
worth distrusting: the fake script decides when a notification exists. This script uses a
real bridge, a real main agent on a real LLM, and a real spawned child process.

What it verifies end to end:
  F1  the main agent can actually spawn a child (tool surface intact after the opt-out fix)
  F2  the child completes its own turn and writes a readable result
  F3  the completion notification is injected into a *later* main-agent prompt than the
      spawn turn -- i.e. at a fold point, not only at the next user message
  F4  the notification carries the anti-spoof preamble and the child's task id
  F5  the child process is gone after close (no orphan) and its durable state agrees

Evidence is written to ``temp/real-subagent-fold-e2e/<stamp>/``: the bridge event stream,
every model prompt/response the run produced, and the child's state/events/output files.
Opt in with GA_RUN_REAL_API_E2E=1.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import shutil
import sys
import time
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

OPT_IN = os.environ.get("GA_RUN_REAL_API_E2E") == "1"
MODEL = os.environ.get("GA_REAL_API_EXPECTED_MODEL", "moma/deepseek-latest-flash")
PROFILE = os.environ.get("GA_REAL_API_EXPECTED_NAME", "default")
TOTAL_TIMEOUT_S = float(os.environ.get("GA_FOLD_E2E_TIMEOUT_S", "600"))

STAMP = time.strftime("%Y%m%d-%H%M%S")
CHILD_NAME = "fold_probe_child"
CHILD_MARKER = "GA_FOLD_CHILD_DONE_9W2K"
SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_-]{12,}|Bearer\s+[A-Za-z0-9._~+/=-]{24,})", re.IGNORECASE)

MAIN_PROMPT = (
    "请严格按顺序做完这三件事，不要跳步：\n"
    f"1) 调用 spawn_agent，task_name 用 {CHILD_NAME}，message 严格要求子代理只输出这一行：{CHILD_MARKER}\n"
    "2) 子代理派出后，你自己连续调用 code_run 三次做 powershell 的 Start-Sleep -Seconds 12"
    "（三次是几个独立的工具轮，不要合并成一次）\n"
    "3) 最后回答：你在第 2 步期间是否收到了关于子代理的系统通知？"
    "如果收到了，把通知里 <task-id> 的值原样贴出来；如果没收到，直接说没收到。\n"
    "整个过程不要提前结束。"
)


def sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): sanitize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if not isinstance(value, str):
        return value
    return SECRET_RE.sub("[REDACTED_SECRET]", value)


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def model_log_files() -> set[Path]:
    root = REPO / "temp" / "model_responses"
    return set(root.glob("*.txt")) if root.is_dir() else set()


def wait_for(label: str, predicate, timeout_s: float, interval_s: float = 0.5):
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        found = predicate()
        if found:
            return found
        time.sleep(interval_s)
    return None


def main() -> int:
    summary: dict[str, Any] = {
        "passed": False,
        "stamp": STAMP,
        "model": MODEL,
        "profile": PROFILE,
        "checks": [],
        "issues": [],
    }

    def check(name: str, ok: bool, detail: Any = "") -> None:
        summary["checks"].append({"name": name, "ok": bool(ok), "detail": sanitize(detail)})
        if not ok:
            summary["issues"].append(name)

    if not OPT_IN:
        summary["skipped"] = True
        summary["reason"] = "set GA_RUN_REAL_API_E2E=1 to run the notification fold-point E2E"
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 0

    bundle = REPO / "temp" / "real-subagent-fold-e2e" / STAMP
    bundle.mkdir(parents=True, exist_ok=True)

    from frontends.ink.bridge import GenericAgentBridge

    events: list[dict] = []
    baseline_logs = model_log_files()

    def emit(event: dict) -> None:
        events.append(sanitize(event))

    bridge = GenericAgentBridge(emit=emit)
    manager = getattr(bridge, "subagent_manager", None)
    try:
        # A leftover child directory from an earlier run would make the name resolve to
        # ``fold_probe_child_1``; clear every candidate so the assertions read this run.
        if manager is not None:
            task_root = Path(getattr(manager, "task_root", ""))
            if task_root.is_dir():
                for stale in sorted(task_root.glob(CHILD_NAME + "*")):
                    shutil.rmtree(stale, ignore_errors=True)
        stale_legacy = REPO / "temp" / CHILD_NAME
        if stale_legacy.exists():
            shutil.rmtree(stale_legacy, ignore_errors=True)

        task_id = bridge.submit(MAIN_PROMPT)
        check("bridge accepted the task", task_id > 0, task_id)
        started = time.monotonic()
        bridge.wait_for_idle(timeout=TOTAL_TIMEOUT_S)
        summary["elapsedSeconds"] = round(time.monotonic() - started, 1)

        # ---- F1/F2: the child was spawned and finished its own turn ----------------
        # Take the child's location from the manager, not from a guessed path: on the
        # product layout children live in the project bucket, not under temp/.
        task_dir = None
        if manager is not None:
            task_root = Path(getattr(manager, "task_root", ""))
            # spawn_agent may have suffixed the name if a previous run left one behind.
            for candidate in sorted(task_root.glob(CHILD_NAME + "*")) if task_root.is_dir() else []:
                if (candidate / "state.json").exists():
                    task_dir = candidate
                    break
        if task_dir is None:
            task_dir = REPO / "temp" / CHILD_NAME

        state_path = task_dir / "state.json"
        wrote_state = wait_for("child state", state_path.exists, 60)
        check("child task dir exists", bool(wrote_state), str(task_dir))
        state = json.loads(state_path.read_text(encoding="utf-8", errors="replace")) if state_path.exists() else {}
        summary["child"] = {
            "taskName": state.get("task_name"),
            "runId": state.get("run_id"),
            "pid": state.get("pid"),
            "processStatus": state.get("process_status"),
            "turnStatus": state.get("turn_status"),
            "capabilityProfile": state.get("capability_profile"),
            "finalOutputRef": state.get("final_output_ref"),
        }
        outputs = sorted(task_dir.glob("output*.txt"))
        child_text = "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in outputs)
        check("child produced a turn output", bool(outputs), [p.name for p in outputs])
        check("child answered without reporting a missing tool", CHILD_MARKER in child_text, CHILD_MARKER)

        # ---- F3/F4: the notification landed mid-turn -------------------------------
        # Only prompts the main agent itself wrote can carry the main-agent notice; the
        # child writes its own log file. Diff the set so an unrelated session cannot pass.
        #
        # "Mid-turn" is decided from the message itself, not from a "Step N" banner: the
        # banner is display text and never reaches the model. A fold-point notice rides on
        # a message that also carries tool_result blocks -- i.e. it was folded into the
        # tool round that just finished, rather than prepended to a fresh user turn.
        new_logs = sorted(model_log_files() - baseline_logs)
        summary["promptLogs"] = [p.name for p in new_logs]
        notices: list[dict] = []
        for path in new_logs:
            text = path.read_text(encoding="utf-8", errors="replace")
            for block in text.split("=== Prompt ===")[1:]:
                if "[GA_SUBAGENT_NOTIFICATIONS]" not in block:
                    continue
                notices.append(
                    {
                        "file": path.name,
                        "foldedOntoToolResult": '"type": "tool_result"' in block,
                        "hasPreamble": "[SYSTEM NOTIFICATION - NOT USER INPUT]" in block,
                        "hasChildId": CHILD_NAME in block,
                        "hasMarker": CHILD_MARKER in block,
                        "chars": len(block),
                    }
                )
        summary["notices"] = notices
        check("a notification was injected into a main-agent prompt", bool(notices), len(notices))
        folded = [n for n in notices if n["foldedOntoToolResult"]]
        check(
            "the notification landed at a fold point, not at a turn start",
            bool(folded),
            [{"file": n["file"], "folded": n["foldedOntoToolResult"]} for n in notices],
        )
        check("notification carries the anti-spoof preamble", any(n["hasPreamble"] for n in notices))
        check("notification names the child task", any(n["hasChildId"] for n in notices))

        # ---- the main agent says it saw it ----------------------------------------
        done = next((e for e in reversed(events) if e.get("type") == "assistant_done"), None)
        main_text = str((done or {}).get("text") or "")
        summary["mainAnswer"] = main_text[-600:]
        check("main agent's final answer is non-empty", bool(main_text.strip()))
        check("main agent acknowledged the notice", CHILD_NAME in main_text or CHILD_MARKER in main_text, main_text[-200:])

        # ---- F5: no orphan --------------------------------------------------------
        if manager is not None:
            with contextlib.suppress(Exception):
                manager.close_agent(CHILD_NAME, reason="fold_e2e_done", grace_s=2.0)
        final_state = json.loads(state_path.read_text(encoding="utf-8", errors="replace")) if state_path.exists() else {}
        pid = final_state.get("pid")
        alive = None
        if pid:
            import psutil

            alive = psutil.pid_exists(int(pid))
        summary["childAfterClose"] = {"pid": pid, "alive": alive, "processStatus": final_state.get("process_status")}
        check("child process exited", alive is not True, summary["childAfterClose"])

        summary["passed"] = not summary["issues"]
        return 0 if summary["passed"] else 2
    except Exception as exc:  # noqa: BLE001 - the driver reports, it does not raise
        summary["error"] = sanitize(f"{type(exc).__name__}: {exc}")
        summary["issues"].append("exception")
        return 1
    finally:
        with contextlib.suppress(Exception):
            (bundle / "bridge-events.jsonl").write_text(
                "\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n",
                encoding="utf-8",
                newline="\n",
            )
            for path in sorted(model_log_files() - baseline_logs):
                with contextlib.suppress(OSError):
                    shutil.copy2(path, bundle / path.name)
            for name in ("state.json", "events.jsonl", "startup.jsonl"):
                source = task_dir / name
                if source.exists():
                    with contextlib.suppress(OSError):
                        shutil.copy2(source, bundle / ("child-" + name))
            for path in sorted(task_dir.glob("output*.txt")):
                with contextlib.suppress(OSError):
                    shutil.copy2(path, bundle / ("child-" + path.name))
            summary["bundle"] = str(bundle)
            print(json.dumps(summary, ensure_ascii=False, indent=2))
            with contextlib.suppress(OSError):
                (bundle / "summary.json").write_text(
                    json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n"
                )


if __name__ == "__main__":
    raise SystemExit(main())
