"""Probe: verify the INSTALLED (site-packages) GenericAgent, not the source checkout.

WHY THIS FILE IS KEPT
---------------------
`pip install -e .` / running from the repo root makes every self-test silently exercise
the SOURCE tree, so a wheel that is missing a package, a vendored asset or a resource
still looks healthy. This probe removes that blind spot by asserting where every module
actually came from before it exercises the runtime.

WHAT IT MEASURES
----------------
1. Import provenance: `genericagent.*`, `ga_cli.*` and `frontends.ink.bridge` must all
   resolve under site-packages, and NO sys.path entry that would win the import may sit
   inside the source checkout. A control group proves the detector can actually fire.
2. Bundled runtime resources resolve: prompt/schema assets, the workflow-authoring skill,
   `llm.yaml.example`, and the vendored acorn that `script_contract.js` requires. These are
   read lazily at launch, so a missing one only shows up on a real run.
3. CLI surface reachable from outside the repo (`ga --version`, `ga list`, `ga status`).
4. Blank bridge boot: construct the real `GenericAgentBridge` and collect `model_status`
   plus `team_roster`. This is the structural half of tests/real_ink_selftest.py, which
   cannot run against an installed wheel because it lives in the repo's `tests/`.
5. (opt-in) one real LLM turn, judged only by structural invariants.

ROOT CAUSE IT GUARDS
--------------------
`scripts/build_release.ps1` audits a hand-written list of wheel entries plus an import
smoke of 6 modules. It cannot catch a *resource* that the runtime reads lazily at
`ga` launch time (prompt assets, skills, the vendored acorn the workflow preflight
requires) -- those only fail on a real launch. See build_release.ps1:190-233.

WHY IT IS DETERMINISTIC
-----------------------
Checks 1-4 need no network and no credentials beyond a readable `llm.yaml` for
`model_status`; they either pass or fail on facts about file locations and event shape.
The optional turn in check 5 is the only non-deterministic part and is gated behind
`GA_PROBE_REAL_TURN=1`.

HOW TO RUN (deliberately NOT from the repo root)
------------------------------------------------
    cd /d/tmp   # any dir outside the checkout
    python /d/git_codes/GenericAgent/tests/probes/python/probe_installed_package.py
    GA_PROBE_REAL_TURN=1 GA_PROBE_PROMPT='Reply with exactly one word: PONG' \
        python /d/git_codes/GenericAgent/tests/probes/python/probe_installed_package.py

This probe does NOT chdir. `python <script>` always prepends the script's own directory
(inside the checkout) to sys.path, which is why check 1 asks whether an entry *contains*
the package rather than merely living in the repo. Running from the repo root would put
the checkout itself on sys.path and the run would then correctly report the shadowing.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
OPT_IN = os.environ.get("GA_PROBE_REAL_TURN") == "1"
PROMPT = os.environ.get("GA_PROBE_PROMPT", "Reply with exactly one word: PONG")
TIMEOUT = float(os.environ.get("GA_PROBE_TIMEOUT", "240"))

_checks: list[dict] = []


def log(message: str = "") -> None:
    # The GA agent hijacks sys.stdout, so keep the original handle.
    sys.__stdout__.write(str(message) + chr(10))
    sys.__stdout__.flush()


def record(name: str, ok: bool, detail: str = "") -> None:
    _checks.append({"name": name, "ok": bool(ok), "detail": str(detail)})
    log(("PASS " if ok else "FAIL ") + name + ((" - " + detail) if detail else ""))


def _is_repo_path(path: str) -> bool:
    try:
        normalized = os.path.normcase(os.path.abspath(path))
        root = os.path.normcase(os.path.abspath(REPO_ROOT))
    except (OSError, ValueError):
        return False
    return normalized == root or normalized.startswith(root + os.sep)


def _shadows(name: str, entry: str) -> bool:
    """True only if `entry` contains a top-level `name` that would win the import.

    `python <script>` always prepends the script's own directory to sys.path, and this
    probe deliberately lives inside the checkout. That entry is harmless because it holds
    no package named `genericagent`/`ga_cli`/`frontends`, so matching on the path alone
    would report a shadow that cannot happen. The real question is whether an entry
    *contains the package*.
    """
    candidate = os.path.join(entry, name)
    return os.path.isdir(candidate) or os.path.isfile(candidate + ".py")


def check_shadow_detector_control() -> None:
    """Control group for the shadow detector itself.

    A "clean" result above is worthless unless the detector can actually fire. Build the
    failing condition explicitly: a directory that DOES contain a `genericagent` package
    must be reported as shadowing, and one that merely sits inside the repo (like this
    probe's own directory) must not. Without this, a detector that always returns False
    would look identical to a correctly-installed wheel.
    """
    with tempfile.TemporaryDirectory(prefix="ga-shadow-control-") as scratch:
        os.makedirs(os.path.join(scratch, "genericagent"))
        positive = _shadows("genericagent", scratch)
        os.makedirs(os.path.join(scratch, "empty-dir"), exist_ok=True)
        negative = _shadows("genericagent", os.path.join(scratch, "empty-dir"))
    record(
        "shadow detector fires on a real package dir (control)",
        positive,
        "dir containing genericagent/ -> %s" % positive,
    )
    record(
        "shadow detector ignores a package-less dir (control)",
        not negative,
        "dir without genericagent/ -> %s" % negative,
    )


def check_provenance() -> None:
    """Every GA module must come from site-packages, not the checkout."""
    import frontends.ink.bridge as bridge
    import ga_cli
    import genericagent

    for label, module in (
        ("genericagent", genericagent),
        ("ga_cli", ga_cli),
        ("frontends.ink.bridge", bridge),
    ):
        origin = getattr(module, "__file__", "") or ""
        record(
            "%s loads from site-packages" % label,
            bool(origin) and not _is_repo_path(origin),
            origin,
        )

    checked_roots = ("genericagent", "ga_cli", "frontends")
    shadowing = [
        entry
        for entry in sys.path
        if entry and _is_repo_path(entry) and any(_shadows(root, entry) for root in checked_roots)
    ]
    record(
        "no sys.path entry can shadow the installed packages",
        not shadowing,
        "; ".join(shadowing) or "clean",
    )


def check_bundled_resources() -> None:
    """Resources the runtime reads lazily at launch -- the wheel audit's blind spot.

    `build_release.ps1` only proves these *files* are inside the wheel. It cannot prove the
    installed code resolves them, nor that `script_contract.js` can still `require('acorn')`
    from site-packages: a source checkout always finds the repo-root `node_modules`, so this
    failure is invisible until someone runs a workflow on a real install.
    """
    import genericagent.resources as resources

    resources_dir = os.path.dirname(os.path.abspath(resources.__file__))
    for relative in (
        "assets/tools_schema.json",
        "assets/sys_prompt.txt",
        "skills/workflow-authoring/SKILL.md",
        "skills/workflow-authoring/references/api.md",
        "llm.yaml.example",
    ):
        path = os.path.join(resources_dir, relative)
        record("bundled resource present: %s" % relative, os.path.exists(path), resources_dir)

    contract = os.path.join(
        resources_dir, os.pardir, "workflow", "js", "script_contract.js"
    )
    contract = os.path.normpath(contract)
    if not os.path.exists(contract):
        record("workflow script_contract.js present", False, contract)
        return
    try:
        completed = subprocess.run(
            [
                "node",
                "-e",
                "const m=require(process.argv[1]);"
                "if(typeof m.parseWorkflowScript!=='function'){process.exit(2)}"
                "console.log('ok')",
                contract,
            ],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        record("script_contract.js requires acorn from site-packages", False, "%s: %s" % (type(exc).__name__, exc))
        return
    detail = (completed.stdout or "").strip() or (completed.stderr or "").strip().splitlines()[-1:] or ""
    if isinstance(detail, list):
        detail = detail[0] if detail else ""
    record(
        "script_contract.js requires acorn from site-packages",
        completed.returncode == 0,
        "rc=%d %s" % (completed.returncode, detail),
    )


def check_cli() -> None:
    """The console script must work with a cwd that is not the repo."""
    for args in (["--version"], ["list"], ["status"]):
        try:
            completed = subprocess.run(
                ["ga", *args],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=120,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            record("ga %s runs" % " ".join(args), False, "%s: %s" % (type(exc).__name__, exc))
            continue
        output = (completed.stdout or "") + (completed.stderr or "")
        record(
            "ga %s runs" % " ".join(args),
            completed.returncode == 0 and bool(output.strip()),
            "rc=%d first line=%r" % (completed.returncode, output.strip().splitlines()[0] if output.strip() else ""),
        )


def check_bridge_boot() -> dict:
    """Boot the real bridge against the installed package and read the two structural surfaces."""
    from frontends.ink.bridge import GenericAgentBridge

    events: list[dict] = []

    def emit(event: dict) -> None:
        events.append(event)

    def wait_for(event_type: str, timeout: float):
        deadline = time.time() + timeout
        while time.time() < deadline:
            hits = [event for event in events if event.get("type") == event_type]
            if hits:
                return hits[-1]
            time.sleep(0.1)
        return None

    with tempfile.TemporaryDirectory(prefix="ga-installed-probe-") as scratch:
        # GA_PROJECT_ROOT drives where runtime artifacts land (bridge.py:72), so a scratch
        # dir keeps this probe from writing sessions/registry files into the checkout.
        os.environ["GA_PROJECT_ROOT"] = scratch
        bridge = GenericAgentBridge(emit=emit)
        try:
            bridge.model_status()
            status = wait_for("model_status", 90)
            models = (status or {}).get("models") or []
            current = next((m.get("name") for m in models if m.get("current")), None)
            record("model_status lists models", len(models) > 0, "%d model(s)" % len(models))
            record("a current model is selected", bool(current), str(current))

            bridge.team_list()
            roster = wait_for("team_roster", 60)
            shape_ok = (
                bool(roster)
                and isinstance(roster.get("team"), dict)
                and "members" in roster
                and "tasks" in roster
            )
            detail = "no event"
            if roster:
                detail = "members=%d tasks=%d" % (len(roster.get("members") or []), len(roster.get("tasks") or []))
            record("/team emits team_roster (team/members/tasks)", shape_ok, detail)

            turn: dict = {"skipped": True}
            if OPT_IN:
                errors_before = len([e for e in events if e.get("type") == "error"])
                task_id = bridge.submit(PROMPT)
                bridge.wait_for_idle(timeout=TIMEOUT)
                done = wait_for("assistant_done", 10)
                text = str((done or {}).get("text") or "").strip()
                usage = next((e for e in reversed(events) if e.get("type") == "token_usage"), None)
                errors_after = [e for e in events if e.get("type") == "error"]
                turn = {
                    "skipped": False,
                    "taskId": task_id,
                    "prompt": PROMPT,
                    "assistantText": text,
                    "tokenUsage": usage,
                }
                record("real LLM turn produced assistant text", bool(text), text[:160])
                record("token usage reported", bool(usage), "total=%s" % (usage or {}).get("totalTokens"))
                record(
                    "no new bridge error event",
                    len(errors_after) <= errors_before,
                    (errors_after[-1].get("message", "") if errors_after else ""),
                )
            else:
                log("SKIP real LLM turn (set GA_PROBE_REAL_TURN=1 to include it)")
        finally:
            try:
                bridge.stop()
            except Exception as exc:  # noqa: BLE001 - a failed stop must not mask results
                log("bridge.stop() raised %s: %s" % (type(exc).__name__, exc))

    return {
        "turn": turn,
        "eventCounts": {t: len([e for e in events if e.get("type") == t]) for t in sorted({str(e.get("type")) for e in events})},
    }


def main() -> int:
    started = time.time()
    log("repo root (for shadow detection): %s" % REPO_ROOT)
    log("cwd                             : %s" % os.getcwd())
    log("")

    check_provenance()
    check_shadow_detector_control()
    log("")
    check_bundled_resources()
    log("")
    check_cli()
    log("")
    extra = check_bridge_boot()

    bundle_dir = os.path.join(REPO_ROOT, "tests", "probes", "captures")
    os.makedirs(bundle_dir, exist_ok=True)
    summary = {
        "startedAt": started,
        "elapsedSeconds": round(time.time() - started, 1),
        "repoRoot": REPO_ROOT,
        "cwd": os.getcwd(),
        "optIn": OPT_IN,
        "python": sys.version,
        "checks": _checks,
        **extra,
    }
    summary_path = os.path.join(bundle_dir, "installed-package-probe-%s.json" % time.strftime("%Y%m%d-%H%M%S"))
    with open(summary_path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(summary, ensure_ascii=False, indent=2))

    failed = [check for check in _checks if not check["ok"]]
    log("")
    log("summary: %s" % summary_path)
    log("VERDICT: %s (%d checks, %d failed)" % ("OK" if not failed else "FAIL", len(_checks), len(failed)))
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(main())
