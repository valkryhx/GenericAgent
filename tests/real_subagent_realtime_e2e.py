"""Real-API E2E for the realtime IPC path (S1/S2 + R1/R2/R4).

The unit suite proves each piece in isolation, but every realtime test either mocks the
owner check or drives `run_task_worker_loop` in-process. This script exercises the one
configuration that no test covers end to end: a real spawned child process, over a real
named pipe / unix socket, authenticated with a real per-run authkey, validated by the real
owner check, receiving a real parent-sent follow-up that drives a real LLM turn.

Opt in with GA_RUN_REAL_API_E2E=1. Model defaults to claude-opus-5 (provider gorouter).
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


EXPECTED_MODEL = os.environ.get("GA_REAL_API_EXPECTED_MODEL", "claude-opus-5")
EXPECTED_PROFILE = os.environ.get("GA_REAL_API_EXPECTED_NAME", "cc-opus-5")
OPT_IN = os.environ.get("GA_RUN_REAL_API_E2E") == "1"
SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_-]{12,}|Bearer\s+[A-Za-z0-9._~+/=-]{24,})", re.IGNORECASE)

FIRST_MARKER = "GA_REALTIME_E2E_FIRST_20260729"
FOLLOWUP_MARKER = "GA_REALTIME_E2E_FOLLOWUP_20260729"


def sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): sanitize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if not isinstance(value, str):
        return value
    return SECRET_RE.sub("[REDACTED_SECRET]", value)


def resolve_profile() -> tuple[int, dict]:
    from llm_client import load_clients_from_yaml

    captured = io.StringIO()
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        clients, _active, _cfg_path, _mtime = load_clients_from_yaml(start_dir=REPO)
    for idx, client in enumerate(clients):
        backend = getattr(client, "backend", None)
        name, model = getattr(backend, "name", ""), getattr(backend, "model", "")
        if model == EXPECTED_MODEL and (name == EXPECTED_PROFILE or EXPECTED_PROFILE in {"", "*"}):
            return idx, {"index": idx, "name": name, "model": model}
    raise RuntimeError(f"profile not found for model={EXPECTED_MODEL} name={EXPECTED_PROFILE}")


def wait_for(predicate, timeout_s: float, interval_s: float = 0.5):
    """Poll until predicate returns a truthy value; return it, or None on timeout."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        result = predicate()
        if result:
            return result
        time.sleep(interval_s)
    return None


def read_state(task_dir: Path) -> dict:
    try:
        return json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
    except Exception:
        return {}


def output_contains(task_dir: Path, marker: str):
    """True once any output*.txt in the task dir carries the marker."""
    for path in sorted(task_dir.glob("output*.txt")):
        try:
            if marker in path.read_text(encoding="utf-8", errors="replace"):
                return path.name
        except OSError:
            continue
    return None


def scan_for_secret_leaks(task_dir: Path, authkey: bytes) -> list[str]:
    """The authkey must never appear anywhere the LLM or a log reader can see it."""
    leaks = []
    hex_key = authkey.hex()
    for path in sorted(task_dir.rglob("*")):
        if not path.is_file() or path.name == "ipc_authkey":
            continue
        try:
            blob = path.read_bytes()
        except OSError:
            continue
        if authkey in blob or hex_key.encode() in blob.lower():
            leaks.append(path.name)
    return leaks


def main() -> int:
    summary: dict[str, Any] = {
        "passed": False,
        "skipped": False,
        "expectedModel": EXPECTED_MODEL,
        "expectedProfile": EXPECTED_PROFILE,
        "issues": [],
    }
    if not OPT_IN:
        summary.update({"skipped": True, "reason": "set GA_RUN_REAL_API_E2E=1 to run the realtime IPC E2E"})
        print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
        return 0

    manager = None
    task_name = None
    try:
        os.environ["GA_SUBAGENT_REALTIME_IPC"] = "1"
        # Coarse poll, long lifetime: if the follow-up lands fast, it can only be the realtime
        # signal that woke the child, not the poll timer.
        os.environ["GA_SUBAGENT_POLL_INTERVAL_S"] = "30"
        os.environ["GA_SUBAGENT_IDLE_TIMEOUT_S"] = "600"

        from subagent_manager import SubagentManager
        from subagent_realtime_ipc import AUTHKEY_FILENAME

        llm_no, profile_row = resolve_profile()
        summary["profile"] = profile_row

        manager = SubagentManager(root_dir=REPO, python_executable=sys.executable)
        if manager.realtime_channel_factory is None:
            summary["issues"].append("realtime_factory_not_enabled")
            raise RuntimeError("GA_SUBAGENT_REALTIME_IPC=1 did not enable the channel factory")

        handle = manager.spawn_agent(
            "realtime_e2e",
            "你是 GA subagent 实时 IPC 回归测试。不要调用工具，不要输出 Markdown。"
            f"只输出这一行精确文本：{FIRST_MARKER}",
            llm_no=llm_no,
            verbose=False,
            parent_session_id="real_realtime_subagent_e2e",
            parent_permission_mode="read_only",
            ipc_mode="socket",
        )
        task_name = handle.task_name
        task_dir = REPO / "temp" / task_name
        summary["handle"] = {
            "taskName": handle.task_name,
            "agentPath": handle.agent_path,
            "runId": handle.run_id,
            "ipcMode": handle.ipc_mode,
            "effectiveIpcMode": handle.effective_ipc_mode,
            "ipcFallbackReason": handle.ipc_fallback_reason,
        }
        if handle.effective_ipc_mode == "file":
            summary["issues"].append("parent_fell_back_to_file_transport")

        authkey_path = task_dir / AUTHKEY_FILENAME
        authkey = authkey_path.read_bytes() if authkey_path.exists() else b""
        summary["authkey"] = {"delivered": bool(authkey), "bytes": len(authkey)}
        if len(authkey) != 32:
            summary["issues"].append("authkey_not_delivered_as_32_bytes")

        # 1) The child must actually subscribe — the R1 defect was a channel nobody connected to.
        subscribed = wait_for(
            lambda: read_state(task_dir).get("child_ipc_status") in {"subscribed", "fallback"}, timeout_s=120
        )
        state = read_state(task_dir)
        summary["childIpc"] = {
            "status": state.get("child_ipc_status"),
            "fallbackReason": state.get("child_ipc_fallback_reason"),
            "address": state.get("child_ipc_address"),
        }
        if not subscribed or state.get("child_ipc_status") != "subscribed":
            summary["issues"].append("child_did_not_subscribe_over_realtime")
        summary["subscriberCount"] = manager._realtime_channels[str(handle.agent_path)].subscriber_count
        if summary["subscriberCount"] < 1:
            summary["issues"].append("channel_has_no_subscriber")

        # 2) First turn completes on a real LLM call.
        first = wait_for(lambda: output_contains(task_dir, FIRST_MARKER), timeout_s=300)
        summary["firstOutputFile"] = first
        if not first:
            summary["issues"].append("first_turn_marker_missing")
            summary["firstTurnState"] = sanitize(read_state(task_dir))
            raise RuntimeError("first turn did not produce its marker")

        waiting = wait_for(lambda: read_state(task_dir).get("process_status") == "waiting_reply", timeout_s=120)
        if not waiting:
            summary["issues"].append("child_never_reached_waiting_reply")

        # 3) The realtime signal is the point: with a 30s poll interval, a follow-up that
        #    starts its turn in a couple of seconds can only have been woken by the channel.
        sent_at = time.monotonic()
        row = manager.followup_task(handle.task_name, f"现在只输出这一行精确文本：{FOLLOWUP_MARKER}")
        summary["followup"] = {"messageId": row.get("message_id"), "eventSeq": row.get("event_seq")}
        consumed = wait_for(
            lambda: any(
                json.loads(line).get("type") == "message_consumed"
                for line in (task_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
                if line.strip()
            ),
            timeout_s=120,
            interval_s=0.2,
        )
        summary["followupWakeSeconds"] = round(time.monotonic() - sent_at, 2)
        if not consumed:
            summary["issues"].append("followup_never_consumed")
        elif summary["followupWakeSeconds"] >= 25:
            # Slower than ~25s means the poll timer delivered it and the channel did nothing.
            summary["issues"].append("followup_woke_on_poll_not_realtime")

        second = wait_for(lambda: output_contains(task_dir, FOLLOWUP_MARKER), timeout_s=300)
        summary["followupOutputFile"] = second
        if not second:
            summary["issues"].append("followup_turn_marker_missing")

        # 4) S1/S2 leak guard: the key must not be readable from anything but its sidecar.
        leaks = scan_for_secret_leaks(task_dir, authkey) if authkey else []
        summary["authkeyLeaks"] = leaks
        if leaks:
            summary["issues"].append("authkey_leaked_into_task_files")
        endpoint = read_state(task_dir).get("ipc_endpoint") or {}
        summary["endpointKeys"] = sorted(endpoint.keys())
        if any("key" in k.lower() or "auth" in k.lower() for k in endpoint):
            summary["issues"].append("ipc_endpoint_exposes_authkey")

        # 5) Closing the channel must delete the key: it can no longer authenticate anything.
        manager.close_agent(handle.task_name, reason="realtime_e2e_done", grace_s=1.0)
        summary["authkeyRemovedOnClose"] = not authkey_path.exists()
        if authkey_path.exists():
            summary["issues"].append("authkey_survived_channel_close")

        summary["passed"] = not summary["issues"]
        print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
        return 0 if summary["passed"] else 2
    except Exception as exc:
        summary["error"] = sanitize(f"{type(exc).__name__}: {exc}")
        if "exception" not in summary["issues"]:
            summary["issues"].append("exception")
        print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
        return 1
    finally:
        if manager is not None and task_name:
            with contextlib.suppress(Exception):
                manager.close_agent(task_name, reason="realtime_e2e_cleanup", grace_s=0.5)


if __name__ == "__main__":
    raise SystemExit(main())
