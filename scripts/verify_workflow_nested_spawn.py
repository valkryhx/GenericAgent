"""Does a workflow child, whose spawn_agent schema has no `role`, still spawn successfully?

This is the one question a 2-child workflow does not answer: those children only wrote
files. Here a real model inside a workflow child is asked to spawn a grandchild, so the
child has to fill `task_name` + `message` from the schema it actually gets
(`assets/tools_schema.json`, i.e. without `role`).

What "no role" could plausibly break, and what is checked here
--------------------------------------------------------------
  1. **It is not required, so it cannot block the call.** Verified statically first: the
     child's `required` is `["task_name", "message"]` — `role` was never in it.
  2. **The model must not stall looking for a role.** The grandchild either appears or
     it does not; if the child invents `role` the handler still accepts it (the request
     alias survives), so a stale habit is not fatal either.
  3. **Delivery, not absence of error.** The grandchild must write a file containing a
     token that exists only in this run, and its `state.json` must reach a terminal
     `completion_status` of succeeded/completed.

Judgement is on the sentinel, not on "no error appeared" — an orchestration call that
silently never ran would otherwise look identical to one that ran.

Usage:
    python scripts/verify_workflow_nested_spawn.py
    python scripts/verify_workflow_nested_spawn.py --timeout 420
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import uuid
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))


WORKFLOW_SCRIPT = r"""
export const meta = { name: 'verify-nested-spawn', description: 'a child spawns a grandchild' }
phase('Nested')
const res = await agent(`
You have a spawn_agent tool. Use it to create ONE subagent, then wait for it, then report.

Spawn it exactly like this:
  task_name: "grandchild_${args.tag}"
  message: "Create the file nested_${args.tag}.txt in your working directory containing only this token: ${args.token} . Do not create any other file."

After spawning, wait for it to finish (wait_agent), then reply with one line stating
whether it completed. Do not create the file yourself.
`)
return { summary: String((res && res.summary) || '').slice(0, 300) };
"""


DEAD_COMPLETION = {"succeeded", "completed", "failed", "cancelled", "canceled", "timeout"}


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Workflow child -> grandchild spawn check.")
    parser.add_argument("--profile", default=os.environ.get("GA_WORKFLOW_LLM_PROFILE", "default"))
    parser.add_argument("--timeout", type=float, default=420.0)
    args = parser.parse_args(argv[1:])

    from genericagent.workflow.child_agent import NativeGPTChildAgentRunner
    from genericagent.workflow.models import WorkflowRun
    from genericagent.workflow.runtime import WorkflowRuntime
    from genericagent.workflow.store import WorkflowStore

    tag = uuid.uuid4().hex[:8]
    token = uuid.uuid4().hex
    workspace = Path(tempfile.mkdtemp(prefix="ga-nested-wf-"))
    keep = os.environ.get("GA_VERIFY_KEEP") == "1"
    failures: list[str] = []

    try:
        store = WorkflowStore(root=str(workspace / "store"))
        runner = NativeGPTChildAgentRunner(profile_name=args.profile, enable_tools=True, max_turns=20)
        run = store.create_run(WorkflowRun(
            run_id=f"wf_nested_{tag}", session_id="nested-spawn-verify",
            script=WORKFLOW_SCRIPT, status="running",
        ))
        previous = os.environ.get("GA_WORKFLOW_WORKSPACE_ROOT")
        os.environ["GA_WORKFLOW_WORKSPACE_ROOT"] = str(workspace)
        try:
            WorkflowRuntime(store=store, runner=runner, timeout_seconds=args.timeout).run(
                run, args={"tag": tag, "token": token, "workspacePath": str(workspace)},
            )
        finally:
            if previous is None:
                os.environ.pop("GA_WORKFLOW_WORKSPACE_ROOT", None)
            else:
                os.environ["GA_WORKFLOW_WORKSPACE_ROOT"] = previous

        loaded = store.load_run(run.run_id)
        print(f"  workflow status : {loaded.status}")
        print(f"  jobs            : {[j.status for j in loaded.jobs]}")
        if loaded.status != "succeeded":
            failures.append(f"workflow status={loaded.status!r} error={str(loaded.error)[:300]!r}")

        # Where the grandchild actually lives.
        #
        # NOT under the workflow workspace: `spawn_agent` from a workflow child goes
        # through `GenericAgentHandler._get_subagent_manager`, which builds its own
        # SubagentManager rooted at the *project* tree — the workflow's
        # `workspace_root` / PathACL does not follow a spawn. So the first version of
        # this script searched the workspace, found nothing, and reported "the child's
        # spawn_agent did not run" while the grandchild had in fact spawned, worked and
        # completed. Read the task_dir out of the child's own transcript instead of
        # assuming where it lands.
        def grandchild_task_dir() -> Path | None:
            # Use the store's own layout helper instead of re-deriving the path:
            # `<root>/<session_id>/workflows/<run_id>/agents/<job_id>/transcript.jsonl`.
            try:
                run_dir = store._run_dir(run)
            except Exception:
                return None
            transcript = run_dir / "agents" / "agent_1" / "transcript.jsonl"
            if not transcript.is_file():
                return None
            for line in transcript.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    event = json.loads(line)
                except Exception:
                    continue
                if event.get("type") == "tool_result" and event.get("toolName") == "spawn_agent":
                    data = event.get("data") or {}
                    if data.get("status") == "started" and data.get("task_dir"):
                        return Path(data["task_dir"])
            return None

        task_dir = grandchild_task_dir()
        if task_dir is None:
            failures.append("the child's transcript has no successful spawn_agent result")
            print("  grandchild task_dir   : <none>")
        else:
            print(f"  grandchild task_dir   : {task_dir}")

        # The grandchild writes relative to its own cwd, so look in both places: its
        # task_dir and anywhere under the project/workflow roots.
        expected_name = f"nested_{tag}.txt"
        search_roots = [r for r in (task_dir, workspace, REPO) if r is not None and Path(r).exists()]
        matches = []
        for root in search_roots:
            matches.extend(Path(root).rglob(expected_name))
        if not matches:
            present = sorted(str(p) for p in (task_dir.rglob("*") if task_dir else []) if p.is_file())[:30]
            failures.append(f"the grandchild never produced {expected_name}; files in its task_dir: {present}")
        else:
            content = matches[0].read_text(encoding="utf-8", errors="replace")
            if token not in content:
                failures.append(f"{expected_name} lacks the run-only token (content={content[:160]!r})")
            else:
                print(f"  {expected_name}: run-only token ✔  ({matches[0]})")
                if Path(matches[0]).parent != task_dir:
                    print(f"  note: it was written to {Path(matches[0]).parent}, not its task_dir")

        # The grandchild's own state.json: a spawn that starts but dies is different
        # from one that never ran, and only this file tells them apart.
        if task_dir is not None:
            state_path = Path(task_dir) / "state.json"
            if not state_path.is_file():
                failures.append(f"grandchild has no state.json at {state_path}")
            else:
                state = json.loads(state_path.read_text(encoding="utf-8"))
                completion = str(state.get("completion_status") or "").lower()
                print(f"  grandchild completion : {completion or '<none>'} "
                      f"process={str(state.get('process_status') or '<none>')}")
                if completion not in DEAD_COMPLETION:
                    failures.append(f"grandchild never reached a terminal state: completion={completion!r}")
                elif completion not in {"succeeded", "completed"}:
                    failures.append(f"grandchild ended as {completion!r}")

        print()
        if failures:
            print("✖ FAILURES:")
            for item in failures:
                print(f"   - {item}")
            return 1
        print("VERDICT: ✔ a workflow child (schema without role) spawned a real grandchild")
        return 0
    finally:
        if keep:
            print(f"  workspace kept at {workspace}")
        else:
            try:
                shutil.rmtree(workspace, ignore_errors=True)
            except Exception:
                pass


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
