"""Workflow script contract validation (meta contract + determinism gate).

This is the CC-compatible script preflight used by the model-visible
``workflow`` tool and by script-level ``workflow()`` nesting. It has no
dependency on the (removed) JSON planner: it parses the ``export const meta``
contract with ``workflow_script_contract.js``, syntax-checks the body with
``node --check``, and applies the determinism gate.

Split out of ``workflow_planner.py`` when the JSON planner path was removed in
favour of Claude Code's direct-JS entry.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from genericagent import paths
from typing import Any

from genericagent.workflow.determinism import determinism_error


def workflow_script_contract_preflight(source: str) -> dict[str, Any]:
    """Run the CC-compatible meta/script contract parser in Node."""

    contract_path = paths.WORKFLOW_JS_DIR / "script_contract.js"
    executable = "node.exe" if sys.platform.startswith("win") else "node"
    try:
        completed = subprocess.run(
            [executable, str(contract_path), "--check"],
            input=source,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": f"workflow script contract preflight unavailable: {exc}"}

    stdout = completed.stdout or ""
    stderr = completed.stderr or ""
    payload: dict[str, Any] | None = None
    if stdout.strip():
        try:
            decoded = json.loads(stdout)
            if isinstance(decoded, dict):
                payload = decoded
        except json.JSONDecodeError:
            payload = None
    if payload is not None:
        if payload.get("ok"):
            return payload
        return {"ok": False, "error": str(payload.get("error") or "workflow script contract check failed")}

    detail = (stderr or stdout or "workflow script contract check failed").strip()
    return {"ok": False, "error": f"workflow script contract preflight unavailable: {detail[:2000]}"}


def validate_rendered_workflow_script(script: str) -> dict[str, Any]:
    """Compile a rendered workflow without executing any agent calls."""

    source = str(script or "")
    if not source.strip():
        return {"ok": False, "error": "workflow script is empty"}
    contract = workflow_script_contract_preflight(source)
    if not contract.get("ok"):
        return contract
    script_body = str(contract.get("scriptBody") or "")
    newline = chr(10)
    wrapped = "(async () => {" + newline + script_body + newline + "})()" + newline
    executable = "node.exe" if sys.platform.startswith("win") else "node"
    try:
        completed = subprocess.run(
            [executable, "--check", "-"],
            input=wrapped,
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ok": False, "error": f"workflow script syntax preflight unavailable: {exc}"}
    if completed.returncode != 0:
        detail = (completed.stderr or completed.stdout or "syntax check failed").strip()
        return {"ok": False, "error": detail[:2000]}
    deterministic_error = determinism_error(script_body)
    if deterministic_error:
        return {"ok": False, "error": deterministic_error}
    return {
        "ok": True,
        "meta": contract.get("meta") if isinstance(contract.get("meta"), dict) else {},
        "scriptBody": script_body,
    }
