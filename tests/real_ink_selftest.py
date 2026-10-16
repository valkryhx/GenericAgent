"""GA ink real-API self-test driver.

See the reusable procedure in
``docs/20261015-GA-ink-真实LLM-e2e-自测SOP.md``.

It drives the *real* bridge (real agent, real LLM API when opted in) and writes an
evidence bundle so a human or agent can judge real execution results, real logs
and the real UI-facing event stream -- with no pre-baked expected output.

Env knobs
---------
GA_RUN_REAL_API_E2E=1   include the real LLM turn (default: UI-command checks only)
GA_SELFTEST_PROMPT=...  override the LLM prompt
GA_SELFTEST_MODEL=...   profile/model selector to switch to before the LLM turn
GA_SELFTEST_TIMEOUT=... seconds to wait for the turn (default 240)
"""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FRONTENDS = REPO_ROOT / "frontends"
for _p in (REPO_ROOT, FRONTENDS):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

from sensitive_redaction import sanitize  # noqa: E402
from frontends.ink.bridge import GenericAgentBridge  # noqa: E402

OPT_IN = os.environ.get("GA_RUN_REAL_API_E2E") == "1"
PROMPT = os.environ.get("GA_SELFTEST_PROMPT", "Reply with exactly one word: PONG")
MODEL_SELECTOR = os.environ.get("GA_SELFTEST_MODEL", "").strip()
TIMEOUT = float(os.environ.get("GA_SELFTEST_TIMEOUT", "240"))
BUNDLE_ROOT = REPO_ROOT / "temp" / "ink-selftest"
BACKEND_LOG = REPO_ROOT / "temp" / "ink_bridge_backend.log"


def log(message):
    """Write to the ORIGINAL stdout: the GA agent redirects sys.stdout."""
    sys.__stdout__.write(str(message) + chr(10))
    sys.__stdout__.flush()


class Recorder:
    """Collects the bridge event stream (already sanitized by the bridge)."""

    def __init__(self):
        self.events = []

    def __call__(self, event):
        self.events.append(event)

    def of(self, type_):
        return [event for event in self.events if event.get("type") == type_]

    def last(self, type_):
        rows = self.of(type_)
        return rows[-1] if rows else None


def record(checks, name, ok, detail=""):
    checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})
    log(("PASS " if ok else "FAIL ") + name + ((" - " + str(detail)) if detail else ""))


def _wait_for(rec, type_, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        event = rec.last(type_)
        if event is not None:
            return event
        time.sleep(0.1)
    return None


def run():
    started = time.time()
    rec = Recorder()
    checks = []
    bridge = GenericAgentBridge(emit=rec)

    # 1) model surface
    bridge.model_status()
    status = _wait_for(rec, "model_status", 60)
    models = (status or {}).get("models") or []
    record(checks, "model_status lists models", len(models) > 0, "%d model(s)" % len(models))
    current = next((m.get("name") for m in models if m.get("current")), None)
    record(checks, "a current model is selected", bool(current), str(current))

    # 2) team roster (the /team + /agents UI backend)
    bridge.team_list()
    roster = _wait_for(rec, "team_roster", 30)
    roster_ok = bool(roster) and isinstance(roster.get("team"), dict) and "members" in roster and "tasks" in roster
    detail = "no event"
    if roster:
        detail = "members=%d tasks=%d" % (len(roster.get("members") or []), len(roster.get("tasks") or []))
    record(checks, "/team emits team_roster (team/members/tasks)", roster_ok, detail)

    # 3) real LLM turn (opt-in)
    turn = {"skipped": True}
    if OPT_IN:
        try:
            if MODEL_SELECTOR:
                bridge.model_switch(MODEL_SELECTOR)
                time.sleep(0.5)
            task_id = bridge.submit(PROMPT)
            bridge.wait_for_idle(timeout=TIMEOUT)
            done = _wait_for(rec, "assistant_done", 5)
            text = str((done or {}).get("text") or "")
            usage = rec.last("token_usage")
            turn = {"skipped": False, "taskId": task_id, "prompt": PROMPT, "assistantText": text, "tokenUsage": usage}
            record(checks, "real LLM turn produced assistant text", bool(text.strip()), text.strip()[:160])
            record(checks, "token usage reported", bool(usage), "total=%s" % (usage or {}).get("totalTokens"))
            record(checks, "no bridge error event", rec.last("error") is None, (rec.last("error") or {}).get("message", ""))
        except Exception as exc:  # noqa: BLE001 - the driver reports, it does not raise
            turn = {"skipped": False, "error": "%s: %s" % (type(exc).__name__, exc)}
            record(checks, "real LLM turn completed", False, str(exc))
    else:
        log("SKIP real LLM turn (set GA_RUN_REAL_API_E2E=1 to include it)")

    # evidence bundle
    bundle = BUNDLE_ROOT / time.strftime("%Y%m%d-%H%M%S")
    bundle.mkdir(parents=True, exist_ok=True)
    with (bundle / "events.jsonl").open("w", encoding="utf-8", newline="\n") as fh:
        for event in rec.events:
            fh.write(json.dumps(sanitize(event), ensure_ascii=False) + "\n")
    try:
        lines = BACKEND_LOG.read_text(encoding="utf-8", errors="replace").splitlines()
        log_tail = "\n".join(lines[-80:])
    except OSError:
        log_tail = ""
    (bundle / "backend-log-tail.txt").write_text(log_tail, encoding="utf-8", newline="\n")
    summary = {
        "startedAt": started,
        "elapsedSeconds": round(time.time() - started, 1),
        "optIn": OPT_IN,
        "checks": checks,
        "turn": turn,
        "eventCounts": {t: len(rec.of(t)) for t in sorted({str(e.get("type")) for e in rec.events})},
        "bundle": str(bundle),
    }
    (bundle / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    log("")
    log("Bundle: %s" % bundle)
    return 0 if all(c["ok"] for c in checks) else 1


if __name__ == "__main__":
    raise SystemExit(run())
