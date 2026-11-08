"""Mutation check for the frame selection model.

Reverse verification for the TDD work: a green test suite proves nothing unless it
goes red when the bug it is supposed to catch comes back. Each mutation below is a
plausible regression, not a syntactic break, and the run must catch every one.
"""

import subprocess
import sys
from pathlib import Path

ROOT = Path(r"D:\git_repos\GenericAgent\frontends\ink\ui")

MUTATIONS = [
    (
        "composer row re-derived from layout arithmetic instead of the caret anchor",
        "src/framePlan.ts",
        "const composerRow = Math.max(0, Math.floor(input.composerRow))",
        "const composerRow = headerRows + transcriptRows",
    ),
    (
        "transcript viewport pad dropped, so a short transcript shifts the chrome up",
        "src/framePlan.ts",
        "const transcriptEnd = transcriptStart + transcriptRows",
        "const transcriptEnd = transcriptStart + input.transcriptLines.length",
    ),
    (
        "one global content width for every region",
        "src/framePlan.ts",
        "const transcriptColumns = transcriptContentColumns(canvas)",
        "const transcriptColumns = chromeColumns",
    ),
    (
        "composer rows painted without their gutter",
        "src/frameSelection.ts",
        "text: `${line.gutter}${line.text}`,",
        "text: line.text,",
    ),
    (
        "mouse point re-offset by the header and gutter (the original bug)",
        "src/frameSelection.ts",
        "return { row: Math.floor(y) - 1, col: Math.floor(x) - 1 }",
        "return { row: Math.floor(y) - 1 - 1, col: Math.floor(x) - 1 - 1 }",
    ),
    (
        "start row ignored, so the first row of a drag copies in full",
        "src/frameSelection.ts",
        "const fromCol = row === range.start.row ? range.start.col : target.leftGutter\n    const toCol = row === range.end.row ? range.end.col : rowEndColumn(target)\n    const span = relativeSpan(target, fromCol, toCol)\n    if (!span) continue",
        "const fromCol = target.leftGutter\n    const toCol = row === range.end.row ? range.end.col : rowEndColumn(target)\n    const span = relativeSpan(target, fromCol, toCol)\n    if (!span) continue",
    ),
    (
        "single-click highlight painted, so a click leaves a one-cell block",
        "src/frameSelection.ts",
        "if (!span || span.to <= span.from) continue",
        "if (!span) continue",
    ),
    (
        "empty-selection check dropped",
        "src/frameSelection.ts",
        "return range.start.row === range.end.row && range.start.col === range.end.col",
        "return false",
    ),
    (
        "frame height ignores a composer anchored past the budget",
        "src/framePlan.ts",
        "const total = Math.max(Math.floor(input.rows), headerRows + transcriptRows, composerRow + composerRows)",
        "const total = Math.max(Math.floor(input.rows), headerRows + transcriptRows)",
    ),
]


def run_tests() -> tuple[bool, str]:
    proc = subprocess.run(
        ["npx", "tsx", "--test", "src/frameSelection.test.ts", "src/framePlan.test.ts"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        shell=True,
    )
    return proc.returncode == 0, (proc.stdout or "") + (proc.stderr or "")


def main() -> int:
    baseline_ok, baseline_out = run_tests()
    if not baseline_ok:
        print("BASELINE IS RED -- fix the suite before mutating")
        print(baseline_out[-3000:])
        return 1
    print("baseline green\n")

    survivors = []
    for name, rel, old, new in MUTATIONS:
        path = ROOT / rel
        original = path.read_text(encoding="utf-8")
        if original.count(old) != 1:
            print(f"SKIP (anchor not unique: {original.count(old)}x) :: {name}")
            survivors.append(name)
            continue
        try:
            path.write_text(original.replace(old, new), encoding="utf-8", newline="\n")
            ok, out = run_tests()
        finally:
            path.write_text(original, encoding="utf-8", newline="\n")
        if ok:
            print(f"SURVIVED :: {name}")
            survivors.append(name)
        else:
            fail_line = next(
                (line for line in out.splitlines() if line.startswith(("\u2716", "not ok", "  \u2716"))),
                "(no failure line)",
            )
            print(f"caught   :: {name}\n           -> {fail_line.strip()}")

    print()
    if survivors:
        print(f"{len(survivors)} mutation(s) survived out of {len(MUTATIONS)}")
        return 1
    print(f"all {len(MUTATIONS)} mutations caught")
    return 0


if __name__ == "__main__":
    sys.exit(main())
