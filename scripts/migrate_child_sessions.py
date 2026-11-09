"""Move legacy child transcripts out of the /resume namespace.

Dry-run by default. ``--apply`` performs the moves; nothing is ever deleted and
an existing target is never overwritten. Transcripts written in the last two
minutes are left in place (a live child may still be appending to them), so run
this when no agent team is running.

    python scripts/migrate_child_sessions.py            # report only
    python scripts/migrate_child_sessions.py --apply    # perform the moves
    python scripts/migrate_child_sessions.py --data-dir <bucket> --json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Running ``python scripts/<x>.py`` puts the scripts directory on sys.path, not
# the checkout root, so an installed genericagent would shadow this one.
_REPO_ROOT = str(Path(__file__).resolve().parents[1])
if _REPO_ROOT not in sys.path:
    sys.path.insert(0, _REPO_ROOT)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", default=None,
                        help="project data bucket (default: the current project's bucket)")
    parser.add_argument("--apply", action="store_true", help="perform the moves")
    parser.add_argument("--json", action="store_true", help="machine-readable report")
    args = parser.parse_args(argv)

    from genericagent import paths
    from genericagent.core.session_scope import migrate_child_sessions

    if args.data_dir:
        bucket = Path(args.data_dir).expanduser()
    else:
        bucket = paths.project_roots(project=Path.cwd()).project_data_dir
    if not bucket.exists():
        print(f"no such bucket: {bucket}", file=sys.stderr)
        return 2

    result = migrate_child_sessions(bucket, apply=args.apply)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    mode = "APPLIED" if result["applied"] else "DRY RUN (add --apply to move)"
    print(f"{mode}: {bucket}")
    print(f"  planned={result['planned']} kept={result['kept']} ambiguous={result['ambiguous']}")
    if result.get("moved") is not None:
        print(f"  moved={result['moved']}")
    for move in result["moves"][:20]:
        print(f"  -> {Path(move['session_path']).name}  parent={move['parent_session_id']}"
              f"  ({move['reason']})")
    if len(result["moves"]) > 20:
        print(f"  ... {len(result['moves']) - 20} more")
    for path in result["ambiguous_paths"][:10]:
        print(f"  ? ambiguous, left in place: {Path(path).name}")
    for item in result.get("results") or []:
        if item.get("error"):
            print(f"  ! {Path(item['session_path']).name}: {item['error']}", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
