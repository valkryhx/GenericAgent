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
    output_path = Path(os.environ.get("GA_REAL_OVERVIEW_DETAIL_OUT") or "temp/real-overview-detail.json")
    summary: dict = {"case": "real_workflow_overview_detail_export", "passed": False, "outputPath": str(output_path)}
    if not OPT_IN:
        summary.update({"skipped": True, "reason": "set GA_RUN_REAL_API_E2E=1 to export real detail"})
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
                bridge = GenericAgentBridge(agent_factory=FakeAgent, emit=events.append, workflow_root=tmp)
                script = (
                    "export const meta = { name: 'overview-export-smoke', description: 'read-only review workflow' }" + chr(10)
                    + "phase('Review')" + chr(10)
                    + "const a = await agent('检查 workflow overview UI 数据契约；不要读取 mykey.py、mykey.json、mcp.json；不要提交。', { label: 'contract-review', phase: 'Review' })" + chr(10)
                    + "return await agent('汇总上面的检查结果；不要写代码。', { label: 'synthesis', phase: 'Review' })" + chr(10)
                )
                run_id = bridge.workflow_draft(script)
                bridge.workflow_detail(run_id)
            detail = [event for event in events if event.get("type") == "workflow_detail" and event.get("run", {}).get("runId") == run_id][-1]
            if not detail.get("script"):
                summary.update({"error": "workflow script missing from detail payload", "runId": run_id, "status": detail.get("run", {}).get("status")})
                print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
                return 3
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(sanitize(detail), ensure_ascii=False, indent=2), encoding="utf-8")
            phases = []
            summary.update({"passed": True, "runId": run_id, "phaseCount": len(phases), "status": detail.get("run", {}).get("status")})
            print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
            return 0
    except Exception as exc:
        summary["error"] = str(exc)
        print(json.dumps(sanitize(summary), ensure_ascii=False, indent=2))
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
