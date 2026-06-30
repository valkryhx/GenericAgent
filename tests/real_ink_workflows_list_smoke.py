from __future__ import annotations

import contextlib
import io
import json
import os
import queue
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTENDS = REPO_ROOT / "frontends"
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
if str(FRONTENDS) not in sys.path:
    sys.path.insert(0, str(FRONTENDS))

from sensitive_redaction import sanitize  # noqa: E402
from frontends.ink_bridge import GenericAgentBridge  # noqa: E402

CONFIG_NAME = os.environ.get("GA_REAL_API_CONFIG", "native_oai_config")
EXPECTED_PROFILE_NAME = os.environ.get("GA_REAL_API_EXPECTED_NAME", "gpt-native")
EXPECTED_MODEL = os.environ.get("GA_REAL_API_EXPECTED_MODEL", "gpt-5.5")
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
        from llmcore import reload_mykeys

        cfg = reload_mykeys()[0].get(CONFIG_NAME) or {}
    summary["profile"] = sanitize({"configName": CONFIG_NAME, "name": cfg.get("name"), "model": cfg.get("model")})
    return cfg.get("name") == EXPECTED_PROFILE_NAME and cfg.get("model") == EXPECTED_MODEL


def main() -> int:
    summary: dict = {
        "case": "ink_workflows_list_real_planner_smoke",
        "configName": CONFIG_NAME,
        "expectedModel": EXPECTED_MODEL,
        "passed": False,
    }
    if not OPT_IN:
        summary.update({"skipped": True, "reason": "set GA_RUN_REAL_API_E2E=1 to run real gpt-5.5 workflow list smoke"})
        print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
        return 0
    if not check_profile(summary):
        summary["error"] = "profile mismatch; expected configured gpt-5.5 profile"
        print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
        return 2

    captured = io.StringIO()
    try:
        with tempfile.TemporaryDirectory() as tmp:
            events = []
            with contextlib.redirect_stdout(captured), contextlib.redirect_stderr(captured):
                os.environ["GA_WORKFLOW_PLANNER_MODE"] = "prompt_guided"
                os.environ["GA_WORKFLOW_PLANNER_CONFIG"] = CONFIG_NAME
                os.environ["GA_WORKFLOW_PLANNER_REPAIR_ATTEMPTS"] = os.environ.get("GA_WORKFLOW_PLANNER_REPAIR_ATTEMPTS", "2")
                bridge = GenericAgentBridge(agent_factory=FakeAgent, emit=events.append, workflow_root=tmp)
                run_id = bridge.workflow_plan(
                    "规划一个低风险只读 workflow list UI 检查计划。边界：不要读取 mykey.py、mykey.json、mcp.json；不要提交。不要写代码。",
                    context={"constraints": ["不要读取 mykey.py、mykey.json、mcp.json", "不要提交", "只读规划"]},
                    auto_approve=False,
                    timeout_seconds=20,
                )
                bridge.workflow_list()
            runs_event = [event for event in events if event.get("type") == "workflow_runs"][-1]
            run_ids = [run.get("runId") for run in runs_event.get("runs") or []]
            listed = next((run for run in runs_event.get("runs") or [] if run.get("runId") == run_id), None)
            summary.update(
                {
                    "runId": run_id,
                    "listedRunCount": len(runs_event.get("runs") or []),
                    "runListed": run_id in run_ids,
                    "listedStatus": listed.get("status") if listed else None,
                    "listedPlannerMode": (listed.get("metadata") or {}).get("plannerMode") if listed else None,
                    "listedTaskType": (listed.get("metadata") or {}).get("workflowTaskType") if listed else None,
                }
            )
            if not listed:
                summary["error"] = "workflow_list did not include real planned run"
                print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
                return 3
            if listed.get("status") != "awaiting_approval":
                summary["error"] = "real planned run should be listed as awaiting_approval"
                print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
                return 4
            summary["passed"] = True
            print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
            return 0
    except Exception as exc:
        summary["error"] = str(exc)
        print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
