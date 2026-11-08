"""Mutation check: would the multi-child tests notice a lost notification?

The tests pass against today's code, which by itself proves nothing -- a test that never
fails is not a test. This script mutates the notification reader in ways a plausible
regression would, and asserts the suite goes red for each one. It restores the original
source afterwards, including on failure.

Mutations, each a realistic way to break it:
  A. keep only the last notification (last-writer-wins)
  B. return the first notification only
  C. render a shared status word for the whole batch
  D. stop acking past the first event (cursor overshoot the other way)

Run: python tests/_mutation_check_multi_notifications.py
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
TARGET = REPO / "genericagent" / "agents" / "notifications.py"
TEST = "tests/test_subagent_multi_notifications.py"

MUTATIONS = {
    "A_keep_only_the_last": (
        "    notifications = _bus(root_dir, **kwargs).peek_notifications()\n",
        "    notifications = _bus(root_dir, **kwargs).peek_notifications()[-1:]\n",
    ),
    "B_first_only": (
        "    notifications = _bus(root_dir, **kwargs).peek_notifications()\n",
        "    notifications = _bus(root_dir, **kwargs).peek_notifications()[:1]\n",
    ),
    "C_shared_status_word": (
        "    blocks = apply_result_budget([\"\\n\".join(_render_task_notification(event)) for event in notifications])\n",
        "    _shared = _status_word(notifications[-1])\n"
        "    blocks = apply_result_budget([\"\\n\".join(_render_task_notification(event)) for event in notifications[:1]])\n"
        "    blocks = [block.replace(_status_word(notifications[0]), _shared) for block in blocks]\n",
    ),
    "D_ack_overshoot": (
        "    return \"\\n\".join(lines), int(notifications[-1].get(\"event_seq\") or 0)\n",
        "    return \"\\n\".join(lines), int(notifications[-1].get(\"event_seq\") or 0) + 999\n",
    ),
}


def run_suite():
    result = subprocess.run(
        [sys.executable, "-m", "pytest", TEST, "-q"],
        cwd=str(REPO),
        capture_output=True,
        text=True,
        # The console default here is GBK, and pytest output contains UTF-8 test names.
        # Decoding with the console codec crashes the subprocess reader and turns a
        # perfectly good RED result into a harness error.
        encoding="utf-8",
        errors="replace",
    )
    return result.returncode, (result.stdout or "") + (result.stderr or "")


def main():
    original = TARGET.read_text(encoding="utf-8")
    lines = []
    out = lines.append
    failures = []

    try:
        code, _ = run_suite()
        out(f"baseline (unmutated): exit={code} (expected 0)")
        if code != 0:
            out("baseline is already failing; mutation results would be meaningless")
            return 2

        for name, (old, new) in MUTATIONS.items():
            if old not in original:
                out(f"MUTATION {name}: SKIPPED (anchor not found -- source drifted)")
                failures.append(name)
                continue
            TARGET.write_text(original.replace(old, new, 1), encoding="utf-8", newline="\n")
            code, output = run_suite()
            caught = code != 0
            out(f"MUTATION {name}: {'CAUGHT (suite red)' if caught else 'MISSED (suite still green)'}")
            if not caught:
                failures.append(name)
                out(output[-800:])
    finally:
        TARGET.write_text(original, encoding="utf-8", newline="\n")
        out("source restored")

    out("")
    out(f"mutations caught: {len(MUTATIONS) - len(failures)}/{len(MUTATIONS)}")
    if failures:
        out(f"missed: {failures}")
    report = Path("D:/tmp/ga_mutation_multi_notifications.txt")
    report.parent.mkdir(parents=True, exist_ok=True)
    report.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    print("\n".join(lines))
    return 0 if not failures else 2


if __name__ == "__main__":
    raise SystemExit(main())
