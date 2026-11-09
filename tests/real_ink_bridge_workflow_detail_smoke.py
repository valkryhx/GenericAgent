from __future__ import annotations

import contextlib
import io
import json
import os
import queue
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTENDS = REPO_ROOT / "frontends"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(FRONTENDS) not in sys.path:
    sys.path.insert(0, str(FRONTENDS))

from genericagent.core.redaction import sanitize  # noqa: E402
from frontends.ink.bridge import GenericAgentBridge  # noqa: E402

EXPECTED_PROFILE_NAME = os.environ.get("GA_REAL_API_EXPECTED_NAME", "default")
EXPECTED_MODEL = os.environ.get("GA_REAL_API_EXPECTED_MODEL", "moma/deepseek-latest-flash")
OPT_IN = os.environ.get("GA_RUN_REAL_API_E2E") == "1"


class FakeBackend:
    def __init__(self):
        self.history = []
        self.last_usage_tokens = None


class FakeClient:
    def __init__(self):
        self.backend = FakeBackend()
        self.last_tools = ""


class FakeAgent:
    def __init__(self):
        self.inc_out = False
        self.verbose = True
        self.is_running = False
        self.history = []
        self.handler = object()
        self.llmclient = FakeClient()
        self.llmclients = [self.llmclient]
        self.llm_no = 0

    def run(self):
        return None

    def put_task(self, text, source="user"):
        q = queue.Queue()
        q.put({"done": ""})
        return q

    def abort(self):
        return None


def check_profile(summary: dict) -> bool:
    captured = io.StringIO()
    with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
        from genericagent.llm.client import load_clients_from_yaml

        clients, _active, config_path, _mtime = load_clients_from_yaml(start_dir=REPO_ROOT)
    target = next((c for c in clients if getattr(c.backend, "name", "") == EXPECTED_PROFILE_NAME), None)
    backend = getattr(target, "backend", None)
    summary["profile"] = sanitize(
        {
            "configName": str(config_path),
            "name": getattr(backend, "name", None),
            "model": getattr(backend, "model", None),
            "backend": type(backend).__name__ if backend is not None else None,
            "available": [getattr(c.backend, "name", "") for c in clients],
        }
    )
    return (
        target is not None
        and summary["profile"]["name"] == EXPECTED_PROFILE_NAME
        and summary["profile"]["model"] == EXPECTED_MODEL
    )


def main() -> int:
    summary: dict = {
        "case": "ink_bridge_workflow_detail_real_planner_contract",
        "expectedProfile": EXPECTED_PROFILE_NAME,
        "expectedModel": EXPECTED_MODEL,
        "passed": False,
    }
    if not OPT_IN:
        summary.update({"skipped": True, "reason": "set GA_RUN_REAL_API_E2E=1 to run real zhanlu-ds bridge detail smoke"})
        print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
        return 0
    if not check_profile(summary):
        summary["error"] = "profile mismatch; expected the zhanlu-ds default profile"
        print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
        return 2

    captured = io.StringIO()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            events = []
            with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
                bridge = GenericAgentBridge(agent_factory=FakeAgent, emit=events.append, workflow_root=tmp)
                # Direct-JS entry (Claude Code model): the model authors the
                # script itself; there is no JSON planner. ``workflow_draft``
                # creates the run without executing it so the detail panel can
                # be inspected first.
                script = (
                    "export const meta = { name: 'ui-detail-smoke', description: 'plan a read-only review workflow' }" + chr(10)
                    + "phase('Review')" + chr(10)
                    + "return await agent('检查 UI 数据契约；不要读取 mykey.py、mykey.json、mcp.json；不要提交。', { label: 'ui-contract-review', phase: 'Review' })" + chr(10)
                )
                run_id = bridge.workflow_draft(script)
                bridge.workflow_detail(run_id)
                time.sleep(0.05)
            detail = [event for event in events if event.get("type") == "workflow_detail" and event.get("run", {}).get("runId") == run_id][-1]
            draft = detail.get("draft")
            progress = detail.get("progress")
            run = detail.get("run") or {}
            summary.update(
                {
                    "runId": run_id,
                    "status": run.get("status"),
                    "launchedBy": (run.get("metadata") or {}).get("launchedBy"),
                    "workflowTaskType": (run.get("metadata") or {}).get("workflowTaskType"),
                    "draftPresent": isinstance(draft, dict),
                    "progressPresent": isinstance(progress, dict),
                    "scriptPresent": bool(detail.get("script")),
                    "scriptLength": len(detail.get("script") or ""),
                    "progressEntries": len((progress or {}).get("workflowProgress") or []) if isinstance(progress, dict) else None,
                    "progressIsNullForAwaitingApproval": progress is None,
                }
            )
            if run.get("status") != "awaiting_approval":
                summary["error"] = "planned run should await approval in smoke"
                print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
                return 3
            if not detail.get("script"):
                summary["error"] = "workflow_detail did not return the direct-JS script"
                print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
                return 4
            progress_entries = ((progress or {}).get("workflowProgress") or []) if isinstance(progress, dict) else None
            if progress is not None and (not isinstance(progress, dict) or progress_entries):
                summary["error"] = "awaiting-approval run must not report executed workflow progress"
                print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
                return 5
            summary["passed"] = True
            print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
            return 0
    except Exception as exc:
        summary["error"] = str(exc)
        print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
