"""Split full-suite failures into environment-caused and candidate real ones.

The suite runs 2224 tests; a run on this machine reported 181 failures. Most of
them trace to a single missing npm module (``acorn``, required by
``genericagent/workflow/js/script_contract.js``) because ``node_modules`` was
never installed at the repo root. That is an environment gap, not a code
regression -- but "most" is not "all", so this groups the failures by module
and by first error line to show what is left.

Usage: python tests/probes/_probe_triage_suite_log.py <unittest log>
"""

import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

HEADER = re.compile(r"^(FAIL|ERROR): (\S+) \((tests\.[A-Za-z0-9_.]+)\)")
FIRST_ERR = re.compile(r"^([A-Za-z_.]*(?:Error|Exception)): (.*)")
ENV_MARKERS = ("acorn", "cjs/loader", "Cannot find module")


def main(path):
    lines = Path(path).read_text(encoding="utf-8", errors="replace").splitlines()
    by_module = defaultdict(Counter)
    env_counter = Counter()
    other = []

    for i, line in enumerate(lines):
        m = HEADER.match(line)
        if not m:
            continue
        test_id = m.group(3)
        module = test_id.rsplit(".", 2)[0]
        msg = ""
        for j in range(i + 1, min(i + 90, len(lines))):
            em = FIRST_ERR.match(lines[j])
            if em:
                msg = em.group(0)
                break
        is_env = any(mark in msg for mark in ENV_MARKERS)
        if is_env:
            by_module[module]["env"] += 1
            env_counter[msg[:70]] += 1
        else:
            by_module[module]["real"] += 1
            other.append((test_id, msg[:160]))

    total_env = sum(c["env"] for c in by_module.values())
    total_real = sum(c["real"] for c in by_module.values())

    print(f"environment-caused (missing npm module): {total_env}")
    print(f"other failures:                          {total_real}")
    print()
    print("by module (module: env / other)")
    for module, c in sorted(by_module.items(), key=lambda kv: -(kv[1]["env"] + kv[1]["real"])):
        print(f"  {module:<62} env={c['env']:<4} other={c['real']}")

    print()
    print("top environment messages:")
    for msg, n in env_counter.most_common(5):
        print(f"  {n:>4}  {msg}")

    print()
    print("non-environment failures:")
    for test_id, msg in other:
        print(f"  {test_id}")
        if msg:
            print(f"      {msg}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1]))
