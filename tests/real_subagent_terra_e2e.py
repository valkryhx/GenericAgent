from __future__ import annotations

import contextlib
import io
import json
import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Any


REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


EXPECTED_MODEL = os.environ.get("GA_REAL_API_EXPECTED_MODEL", "gpt-5.6-terra")
EXPECTED_PROFILE = os.environ.get("GA_REAL_API_EXPECTED_NAME", "terra")
OPT_IN = os.environ.get("GA_RUN_REAL_API_E2E") == "1"
SECRET_RE = re.compile(r"(sk-[A-Za-z0-9_-]{12,}|Bearer\s+[A-Za-z0-9._~+/=-]{24,})", re.IGNORECASE)


def sanitize(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): sanitize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [sanitize(item) for item in value]
    if not isinstance(value, str):
        return value
    return SECRET_RE.sub("[REDACTED_SECRET]", value)


def drain_until(handle, expected_text: str, timeout_s: float = 240.0) -> dict:
    from subagent_manager import SubagentManager

    manager = SubagentManager(root_dir=REPO)
    deadline = time.monotonic() + timeout_s
    last_state = None
    while time.monotonic() < deadline:
        state = manager.read_agent(handle.task_name)
        last_state = {
            "turn_status": state.turn_status,
            "process_status": state.process_status,
            "round": state.round,
            "output_path": state.output_path,
            "final_output_path": state.final_output_path,
            "artifact_dir": state.artifact_dir,
            "run_id": state.run_id,
        }
        if state.final_output_path:
            path = Path(state.final_output_path)
            if path.exists():
                text = path.read_text(encoding="utf-8", errors="replace")
                if expected_text in text:
                    return {"state": last_state, "output": text}
        if state.turn_status == "errored" or state.process_status in {"exited", "shutdown", "killed"}:
            break
        time.sleep(2)
    raise RuntimeError(f"subagent did not produce expected output: {last_state}")


def wait_for_transcript_types(path: Path, required: tuple[str, ...], timeout_s: float = 30.0) -> list[dict]:
    deadline = time.monotonic() + timeout_s
    rows: list[dict] = []
    while time.monotonic() < deadline:
        if path.exists():
            rows = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
            types = [row.get("type") for row in rows]
            if all(item in types for item in required):
                return rows
        time.sleep(1)
    return rows


def main() -> int:
    summary: dict[str, Any] = {
        "passed": False,
        "skipped": False,
        "expectedModel": EXPECTED_MODEL,
        "expectedProfile": EXPECTED_PROFILE,
        "issues": [],
    }
    if not OPT_IN:
        summary.update({"skipped": True, "reason": "set GA_RUN_REAL_API_E2E=1 to run real terra subagent E2E"})
        print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
        return 0

    try:
        import agentmain
        from llm_client import load_clients_from_yaml
        from subagent_artifacts import SubagentArtifactStore
        from subagent_manager import SubagentManager

        captured = io.StringIO()
        with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
            clients, active_index, cfg_path, _mtime_ns = load_clients_from_yaml(start_dir=REPO)
        profiles = []
        for idx, client in enumerate(clients):
            backend = getattr(client, "backend", None)
            profiles.append(
                {
                    "index": idx,
                    "name": getattr(backend, "name", ""),
                    "model": getattr(backend, "model", ""),
                    "active": idx == active_index,
                }
            )
        summary["config"] = {"path": str(cfg_path), "profiles": profiles, "loadLogChars": len(captured.getvalue())}
        profile = next(
            (
                (idx, row)
                for idx, row in enumerate(profiles)
                if row["model"] == EXPECTED_MODEL and (row["name"] == EXPECTED_PROFILE or EXPECTED_PROFILE in {"", "*"})
            ),
            None,
        )
        if profile is None:
            summary["issues"].append("terra_profile_not_found")
            print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
            return 2
        llm_no, profile_row = profile
        marker = "GA_SUBAGENT_TERRA_E2E_OK_20260727"
        resume_marker = "GA_SUBAGENT_TERRA_RESUME_E2E_OK_20260728"
        prompt = (
            "你是 GA subagent 真实 API 回归测试。不要调用工具，不要输出 Markdown。"
            f"只输出这一行精确文本：{marker}"
        )
        resume_prompt = (
            "你正在执行 GA subagent resume_agent 真实 API 回归测试。不要调用工具，不要输出 Markdown。"
            f"只输出这一行精确文本：{resume_marker}"
        )
        with tempfile.TemporaryDirectory(dir=REPO / "temp", prefix="terra_subagent_e2e_"):
            manager = SubagentManager(root_dir=REPO, python_executable=sys.executable)
            handle = manager.spawn_agent(
                "terra_real_subagent",
                prompt,
                llm_no=llm_no,
                verbose=False,
                parent_session_id="real_terra_subagent_e2e",
                parent_permission_mode="read_only",
            )
            summary["handle"] = {
                "taskName": handle.task_name,
                "agentPath": handle.agent_path,
                "runId": handle.run_id,
                "permissionProfile": handle.permission_profile,
                "parentPermissionMode": handle.parent_permission_mode,
                "profile": profile_row,
            }
            drained = drain_until(handle, marker)
            summary["outputPreview"] = drained["output"][:500]
            summary["state"] = drained["state"]
            artifact = SubagentArtifactStore(drained["state"]["artifact_dir"]).get("final_output_round_0")
            summary["artifact"] = {"artifact_id": artifact.get("artifact_id"), "sha256": artifact.get("sha256")}
            transcript_path = REPO / "temp" / "sessions" / "real_terra_subagent_e2e" / "subagents" / f"{handle.run_id}.jsonl"
            # 先关闭第一轮等待中的子进程，再通过 resume_agent 复用同一 run_id 启动第二轮。
            try:
                manager.close_agent(handle.task_name, reason="real_e2e_before_resume", grace_s=0.2)
            except Exception as exc:
                summary["closeWarning"] = sanitize(f"{type(exc).__name__}: {exc}")
            resumed = manager.resume_agent(handle.task_name, resume_prompt)
            summary["resume"] = {
                "target": resumed.target,
                "runId": resumed.handle.run_id,
                "pid": resumed.handle.pid,
                "resumeContextStatus": resumed.resume_context.get("status"),
                "resumeContextEvents": resumed.resume_context.get("source_event_count"),
            }
            resumed_drained = drain_until(resumed.handle, resume_marker)
            summary["resumeOutputPreview"] = resumed_drained["output"][:500]
            summary["resumeState"] = resumed_drained["state"]
            resume_artifact = SubagentArtifactStore(resumed_drained["state"]["artifact_dir"]).get("final_output_round_1")
            summary["resumeArtifact"] = {"artifact_id": resume_artifact.get("artifact_id"), "sha256": resume_artifact.get("sha256")}
            try:
                manager.close_agent(handle.task_name, reason="real_e2e_done", grace_s=0.2)
            except Exception as exc:
                summary["resumeCloseWarning"] = sanitize(f"{type(exc).__name__}: {exc}")
            rows = wait_for_transcript_types(
                transcript_path,
                ("metadata", "request", "assistant", "final_output", "turn_completed", "agent_closed"),
            )
            types = [row.get("type") for row in rows]
            summary["transcriptTypes"] = types
            for required in ("metadata", "request", "assistant", "final_output", "turn_completed", "agent_closed"):
                if required not in types:
                    summary["issues"].append(f"missing_transcript_{required}")
            final_outputs = [row for row in rows if row.get("type") == "final_output"]
            summary["finalOutputRounds"] = [row.get("payload", {}).get("round") for row in final_outputs]
            if artifact.get("artifact_id") != "final_output_round_0":
                summary["issues"].append("artifact_id_mismatch")
            if resume_artifact.get("artifact_id") != "final_output_round_1":
                summary["issues"].append("resume_artifact_id_mismatch")
            if handle.parent_permission_mode != "read_only":
                summary["issues"].append("parent_permission_mode_not_recorded")
            if resumed.handle.run_id != handle.run_id:
                summary["issues"].append("resume_run_id_changed")
            if resumed_drained["state"].get("round") != 1:
                summary["issues"].append("resume_round_not_1")
            if resumed_drained["state"].get("output_path") and not str(resumed_drained["state"].get("output_path")).endswith("output1.txt"):
                summary["issues"].append("resume_output_path_not_output1")
        summary["passed"] = not summary["issues"]
        print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
        return 0 if summary["passed"] else 2
    except Exception as exc:
        summary["error"] = sanitize(f"{type(exc).__name__}: {exc}")
        summary["issues"].append("exception")
        print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
