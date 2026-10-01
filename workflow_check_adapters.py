from __future__ import annotations

import hashlib
import json
import subprocess
import time
from pathlib import Path

from sensitive_redaction import redact_sensitive_text


MAX_OUTPUT_CHARS = 20_000


def _workspace_path(workspace):
    path = Path(workspace).expanduser().resolve()
    if not path.is_dir():
        raise ValueError("workflow check workspace must be an existing directory")
    return path


def _safe_workspace_child(workspace, raw):
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("artifact path must be a non-empty relative path")
    candidate = (workspace / raw).resolve()
    try:
        candidate.relative_to(workspace)
    except ValueError as exc:
        raise ValueError("artifact path must stay within workspace") from exc
    return candidate


def _bounded(value):
    text = redact_sensitive_text(str(value or ""))
    return {"text": text[:MAX_OUTPUT_CHARS], "truncated": len(text) > MAX_OUTPUT_CHARS}


def _command_argv(check):
    command = check.get("command")
    if not isinstance(command, list) or not command or not all(isinstance(item, str) and item for item in command):
        raise ValueError("verification command must be an argv list")
    return list(command)


def _run_command(check, workspace, timeout_s):
    argv = _command_argv(check)
    started = time.time()
    try:
        completed = subprocess.run(
            argv,
            cwd=workspace,
            capture_output=True,
            text=True,
            timeout=max(0.1, float(timeout_s)),
            shell=False,
            check=False,
        )
        stdout = _bounded(completed.stdout)
        stderr = _bounded(completed.stderr)
        return {
            "status": "passed" if completed.returncode == 0 else "failed",
            "evidence": {
                "argv": argv,
                "exitCode": completed.returncode,
                "stdout": stdout["text"],
                "stderr": stderr["text"],
                "stdoutTruncated": stdout["truncated"],
                "stderrTruncated": stderr["truncated"],
                "timedOut": False,
                "durationMs": round((time.time() - started) * 1000, 2),
                "commandFingerprint": hashlib.sha256(json.dumps(argv).encode("utf-8")).hexdigest(),
            },
        }
    except subprocess.TimeoutExpired as exc:
        stdout = _bounded(exc.stdout)
        stderr = _bounded(exc.stderr)
        return {
            "status": "failed",
            "evidence": {
                "argv": argv,
                "exitCode": None,
                "stdout": stdout["text"],
                "stderr": stderr["text"],
                "stdoutTruncated": stdout["truncated"],
                "stderrTruncated": stderr["truncated"],
                "timedOut": True,
                "durationMs": round((time.time() - started) * 1000, 2),
                "commandFingerprint": hashlib.sha256(json.dumps(argv).encode("utf-8")).hexdigest(),
            },
        }


def _schema_check(check):
    evidence = check.get("evidence")
    if not isinstance(evidence, dict):
        return {"status": "failed", "evidence": {"schemaRef": check.get("schemaRef"), "reason": "missing evidence"}}
    required = check.get("requiredFields") or ["verificationPassed", "checks", "blockingIssues"]
    missing = [field for field in required if field not in evidence]
    return {
        "status": "passed" if not missing and evidence.get("verificationPassed", True) is not False else "failed",
        "evidence": {"schemaRef": check.get("schemaRef"), "missingFields": missing, "payload": evidence},
    }


def _artifact_check(check, workspace):
    path = _safe_workspace_child(workspace, check.get("path"))
    exists = path.is_file() if check.get("file", True) else path.exists()
    return {
        "status": "passed" if exists else "failed",
        "evidence": {"path": str(path.relative_to(workspace)), "exists": exists, "size": path.stat().st_size if exists and path.is_file() else 0},
    }


def run_check(check, *, workspace, timeout_s=30):
    if not isinstance(check, dict):
        raise ValueError("verification check must be an object")
    check_id = str(check.get("id") or "").strip()
    if not check_id:
        raise ValueError("verification check requires id")
    workspace_path = _workspace_path(workspace)
    kind = str(check.get("kind") or "").strip().lower()
    if kind == "command":
        result = _run_command(check, workspace_path, timeout_s)
    elif kind == "schema":
        result = _schema_check(check)
    elif kind == "artifact":
        result = _artifact_check(check, workspace_path)
    elif kind in {"diff", "review", "manual"}:
        result = {"status": "skipped", "evidence": {"reason": f"{kind} evidence must be supplied by runtime owner"}}
    else:
        raise ValueError(f"unsupported verification check kind: {kind}")
    result["checkId"] = check_id
    result["kind"] = kind
    return result
