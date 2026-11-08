"""Diff the failing test IDs of two unittest logs.

Counting failures is not enough to attribute them: 181 vs 183 could be the same
set plus noise, or a different set entirely. This prints the IDs only one side
has, so a change can be judged by *which* tests differ rather than by totals.

Usage: python tests/probes/_probe_diff_failures.py <base.log> <new.log>
"""

import re
import sys
from pathlib import Path

HEADER = re.compile(r"^(FAIL|ERROR): (?:\S+ )?\(([A-Za-z0-9_.]+)\)")


def _normalize(test_id):
    """Strip the package prefix so both discovery forms compare equal.

    ``discover -s tests`` imports modules top-level and reports
    ``test_mcp_runtime.X``; ``discover -s tests -t .`` imports them as
    ``tests.test_mcp_runtime.X``. The same test therefore has two spellings,
    and comparing raw IDs would report every failure as new.
    """
    return test_id[len("tests."):] if test_id.startswith("tests.") else test_id


def failing_ids(path):
    ids = set()
    for line in Path(path).read_text(encoding="utf-8", errors="replace").splitlines():
        m = HEADER.match(line)
        if m:
            ids.add(_normalize(m.group(2)))
    return ids


def main(base_path, new_path):
    base = failing_ids(base_path)
    new = failing_ids(new_path)

    print(f"base failing: {len(base)}")
    print(f"new  failing: {len(new)}")
    print()

    only_new = sorted(new - base)
    only_base = sorted(base - new)

    print(f"FAIL only in new ({len(only_new)})  <- candidate regressions:")
    for t in only_new:
        print(f"  + {t}")
    if not only_new:
        print("  (none)")

    print()
    print(f"FAIL only in base ({len(only_base)})  <- fixed or renamed:")
    for t in only_base:
        print(f"  - {t}")
    if not only_base:
        print("  (none)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
