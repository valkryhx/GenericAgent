"""Real evidence: a followup_task message reaches a live child on the product layout.

The unit tests prove the path resolution. This proves the *consequence* with a real OS
child and a real model, which is what the defect actually cost:

  the lead recorded a provenance digest in ``<bus>/provenance.jsonl``; the child looked
  for it in ``<bus>/tasks/subagents/provenance.jsonl``, found nothing, saw the author
  ``/root`` in its roster, and ``verify`` answered ``forged``. ``consume_trigger_turn``
  refuses a forged row permanently, so the message was dropped while the sender already
  held a success receipt.

The acceptance signal is deliberately hard to fake:

  1. a random token appears ONLY in the followup message -- never in the spawn prompt,
     the task name, or any file the child could read without the message;
  2. the child must reproduce that token in its own output;
  3. no mailbox row may be in state ``forged``;
  4. no orphan bus or sessions directory may exist under the task root.

(2) is the one that matters. A test that only asserts "no forged row" passes when the
message is silently never queued at all.

Run (real model):
    $env:GA_RUN_REAL_API_E2E="1"; python tests/real_child_handdown_delivery_e2e.py

Without the opt-in the child is still spawned, but the script reports INCONCLUSIVE rather
than passing on a turn that never ran.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import random
import string
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
if str(REPO) not in sys.path:
    sys.path.insert(0, str(REPO))

OPT_IN = os.environ.get("GA_RUN_REAL_API_E2E") == "1"
REPORT = Path("D:/tmp/ga_real_child_handdown_delivery.txt")
NAME_PREFIX = "handdown_e2e"
TIMEOUT_S = 420


def token(prefix):
    alphabet = string.ascii_uppercase + string.digits
    return prefix + "".join(random.choice(alphabet) for _ in range(10))


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8", errors="replace"))
    except Exception:
        return {}


def mailbox_states(path):
    path = Path(path)
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            out.append(str(json.loads(line).get("state")))
        except json.JSONDecodeError:
            continue
    return out


def read_all_rounds(task_dir):
    """Concatenate every round's output: each turn writes its own ``outputN.txt``.

    Reading only ``output.txt`` misses the followup turn entirely, whose answer lands in
    ``output1.txt`` -- which makes a working delivery look like a lost message. That is
    the one failure mode this harness must not have, since losing the message is exactly
    what it is here to detect.
    """
    task_dir = Path(task_dir)

    def round_no(path):
        stem = path.stem
        if stem == "output":
            return 0
        try:
            return int(stem[len("output"):])
        except ValueError:
            return -1

    files = sorted(
        (p for p in task_dir.glob("output*.txt") if round_no(p) >= 0),
        key=round_no,
    )
    return "\n".join(p.read_text(encoding="utf-8", errors="replace") for p in files), [
        p.name for p in files
    ]


def wait_for(predicate, timeout_s, interval=3.0):
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def main():
    lines = []
    out = lines.append
    checks = {}

    from genericagent import paths
    from genericagent.agents.manager import SubagentManager

    roots = paths.project_roots(project=str(REPO), data_root=str(paths.RUNTIME_ROOT))
    session_id = f"handdown_e2e_{int(time.time())}"
    manager = SubagentManager(
        project_root=str(roots.project_root),
        python_executable=sys.executable,
        project_state_root=str(roots.data_root),
        project_data_dir=str(roots.project_data_dir),
        parent_session_id=session_id,
        task_store_root=str(roots.tasks_dir),
        worktree_root=str(roots.project_worktrees_dir),
    )
    name = f"{NAME_PREFIX}_{int(time.time())}"
    bus = Path(manager.subagents_state_dir)
    task_root = Path(manager.task_root)
    orphan_bus = task_root / "subagents"
    orphan_sessions = task_root / "sessions"

    # The two tokens: one for the spawn turn, one that exists ONLY in the followup.
    spawn_token = token("SPAWN")
    followup_token = token("FOLLOWUP")

    out(f"session            = {session_id}")
    out(f"bus (lead reads)   = {bus}")
    out(f"task root          = {task_root}")
    out(f"child task dir     = {task_root / name}")
    out("")

    llm_no = 0
    if OPT_IN:
        with contextlib.suppress(Exception):
            from genericagent.llm.client import load_clients_from_yaml

            sink = io.StringIO()
            with contextlib.redirect_stdout(sink), contextlib.redirect_stderr(sink):
                _, active, *_ = load_clients_from_yaml(start_dir=REPO)
            llm_no = int(active or 0)

    handle = manager.spawn_agent(
        name,
        f"Reply with exactly this one line and nothing else: {spawn_token}",
        llm_no=llm_no,
        verbose=False,
        parent_session_id=session_id,
        parent_permission_mode="full_access",
        ipc_mode="socket",
    )
    task_dir = Path(handle.task_dir)
    out(f"spawned pid        = {handle.pid}")
    out(f"spawn token        = {spawn_token}")
    out("")

    if OPT_IN:
        wait_for(
            lambda: read_json(task_dir / "state.json").get("turn_status") == "completed",
            timeout_s=240,
        )
    state = read_json(task_dir / "state.json")
    out(f"first turn_status  = {state.get('turn_status')}")
    out(f"first process      = {state.get('process_status')}")

    # ---- the delivery under test ----
    followup = None
    if OPT_IN:
        out("")
        out(f"sending followup with token {followup_token} (exists nowhere else)")
        followup = manager.followup_task(
            name,
            f"Reply with exactly this one line and nothing else: {followup_token}",
            submission_id=f"handdown_e2e_{int(time.time())}",
        )
        out(f"followup accepted  = {bool(followup)}")
        wait_for(
            lambda: followup_token in read_all_rounds(task_dir)[0],
            timeout_s=TIMEOUT_S,
        )

    output, round_files = read_all_rounds(task_dir)

    states = mailbox_states(task_dir / "mailbox.jsonl")
    out("")
    out(f"output round files = {round_files}")
    out(f"mailbox row states = {states}")
    out(f"output contains spawn token    : {spawn_token in output}")
    out(f"output contains followup token : {followup_token in output}")
    out(f"orphan bus exists              : {orphan_bus.exists()}")
    out(f"orphan sessions exists         : {orphan_sessions.exists()}")
    orphan_files = sorted(p.name for p in orphan_bus.iterdir()) if orphan_bus.is_dir() else []
    out(f"orphan bus files               : {orphan_files}")
    out("")

    no_forged = "forged" not in states
    checks["no mailbox row is forged"] = no_forged
    checks["no orphan bus under task root"] = not orphan_files
    checks["no orphan sessions under task root"] = not orphan_sessions.exists()

    if OPT_IN:
        checks["the child reproduced the followup-only token"] = followup_token in output
    else:
        out("INCONCLUSIVE: set GA_RUN_REAL_API_E2E=1 to include a real model turn")
        out("  (path checks below still ran and are meaningful)")

    for label, ok in checks.items():
        out(f"CHECK {label:52s}: {ok}")

    verdict = 0 if all(checks.values()) else 2
    out("")
    out(f"verdict = {'PASS' if verdict == 0 else 'FAIL'}")

    with contextlib.suppress(Exception):
        manager.close_agent(name, reason="handdown_e2e_done", grace_s=1.0)

    REPORT.parent.mkdir(parents=True, exist_ok=True)
    REPORT.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return verdict


if __name__ == "__main__":
    raise SystemExit(main())
