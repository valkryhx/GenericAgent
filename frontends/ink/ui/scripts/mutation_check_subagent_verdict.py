"""Mutation check for the subagent work verdict.

The reported bug has now appeared twice under two different process statuses
(`killed`, then `waiting_reply`), so the guard has to be the *rule* -- never read a
process lifecycle word as a work verdict -- rather than either instance. Each mutation
below reintroduces a plausible version of the misreading.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(r"D:\git_repos\GenericAgent\frontends\ink\ui")
TARGET = "src/agentNotice.ts"

TESTS = ["src/subagentWorkVerdict.test.ts", "src/subagentAnnouncement.test.ts"]

MUTATIONS = [
    (
        "sourceStatus trusted first again (the original ordering)",
        "  const workStatus = metaCompletion\n    || turnStatus\n    || (sourceStatus && !PROCESS_LIFECYCLE_STATUSES.has(sourceStatus) ? sourceStatus : '')",
        "  const workStatus = sourceStatus || metaCompletion || turnStatus",
    ),
    (
        "turnStatus dropped from the verdict chain",
        "  const workStatus = metaCompletion\n    || turnStatus\n    || (sourceStatus && !PROCESS_LIFECYCLE_STATUSES.has(sourceStatus) ? sourceStatus : '')",
        "  const workStatus = metaCompletion\n    || (sourceStatus && !PROCESS_LIFECYCLE_STATUSES.has(sourceStatus) ? sourceStatus : '')",
    ),
    (
        "the lifecycle filter stops filtering (every sourceStatus accepted)",
        "    || (sourceStatus && !PROCESS_LIFECYCLE_STATUSES.has(sourceStatus) ? sourceStatus : '')",
        "    || sourceStatus",
    ),
    (
        "projected status dropped from the end of the chain",
        "    || (sourceStatus && !PROCESS_LIFECYCLE_STATUSES.has(sourceStatus) ? sourceStatus : '')\n    || status",
        "    || (sourceStatus && !PROCESS_LIFECYCLE_STATUSES.has(sourceStatus) ? sourceStatus : '')",
    ),
    (
        "any word treated as a success, so nothing ever reads as failed",
        "  const ok = COMPLETED_STATUSES.has(workStatus)",
        "  const ok = true",
    ),
]


def run_tests() -> tuple[bool, str]:
    """Run the suite twice per mutation.

    Once is not enough here. Two of the guards overlap (the lifecycle filter and the
    success vocabulary), so a mutation that removes one of them still produces the right
    answer -- an *equivalent* mutation, which teaches nothing. A mutation that genuinely
    changes behaviour fails deterministically, so a second identical run would only be
    wasted time there; the repeat is used to make sure any survivor really is equivalent
    rather than a flake.
    """
    results = []
    for _ in range(2):
        proc = subprocess.run(
            ["npx", "tsx", "--test", *TESTS],
            cwd=ROOT,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            shell=True,
        )
        results.append((proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")))
    # Only "green both times" counts as surviving.
    return all(ok for ok, _ in results), results[0][1]


def main() -> int:
    baseline_ok, baseline_out = run_tests()
    if not baseline_ok:
        print("BASELINE IS RED -- fix the suite before mutating")
        print(baseline_out[-3000:])
        return 1
    print("baseline green\n")

    path = ROOT / TARGET
    original = path.read_text(encoding="utf-8")
    survivors = []
    try:
        for name, old, new in MUTATIONS:
            if original.count(old) != 1:
                print(f"SKIP (anchor not unique: {original.count(old)}x) :: {name}")
                survivors.append(name)
                continue
            path.write_text(original.replace(old, new), encoding="utf-8", newline="\n")
            ok, out = run_tests()
            path.write_text(original, encoding="utf-8", newline="\n")
            if ok:
                print(f"SURVIVED :: {name}")
                survivors.append(name)
            else:
                failed = [line.strip() for line in out.splitlines() if line.strip().startswith("\u2716")]
                print(f"caught   :: {name}\n           -> {(failed[0] if failed else '(see output)')}")
    finally:
        path.write_text(original, encoding="utf-8", newline="\n")

    print()
    if survivors:
        print(f"{len(survivors)} mutation(s) survived out of {len(MUTATIONS)}")
        return 1
    print(f"all {len(MUTATIONS)} mutations caught")
    return 0


if __name__ == "__main__":
    sys.exit(main())
