"""Why did `spawn_agent` lose its arguments? Replay a recorded session log.

What it measures
----------------
For every `spawn_agent` call in a session JSONL, the argument keys that actually
reached the handler, compared against what the provider says the model emitted.
The gap between those two lists is the whole diagnosis.

Root cause (2026-11-15, installed 0.6.1, `session_8d931ca1...`)
---------------------------------------------------------------
15 of 24 calls arrived as `{'agent_type': 'explore'}` — every other key gone.
Three checks say it was NOT transport truncation:

* `llm._try_parse_tool_args` emits `{"_raw": ...}` on half a JSON document;
  no call carried `_raw`.
* it emits suffixed ids (`<id>_0`, `split_0`) when concatenated objects are
  split; no id carried a suffix.
* no `max_tokens` / `content_filter` / stream-abort sentinel in the log.

So the JSON was syntactically complete and simply missing keys. The model was
never told those keys were required: `spawn_agent` declared 18 properties and
no `required`, while `agent_type` was the only enum-constrained one (3 role
names + a 304-char catalog injected at runtime by
`agentmain._apply_subagent_role_schema`). An optional field with a closed value
set became the only output anchor. Fix: declare `required` on the schema.

Second defect it exposed: `handler.do_spawn_agent` filled the absent
`task_name` from `role.name` (`handler.py:1761` before the fix), so the first
role spawn took `/root/explore` and the next three died on `name_conflict` —
reporting a path the caller never chose.

Why it is deterministic
-----------------------
Pure replay of a file on disk. No timing, no sampling, no network. The same log
always yields the same table, so a regression in the classification logic shows
up here rather than in a lucky/unlucky live session.

Why it must be kept
-------------------
This is the only evidence that "the model dropped the field" and "the transport
dropped the field" are distinguishable from GA's own logs, and it encodes the
three-way check (_raw / split id / truncation sentinel) that made that call. The
next time a tool call arrives with missing arguments, run this first instead of
re-deriving the discriminator.

Usage
-----
    python tests/probes/python/_probe_spawn_arg_loss.py <session.jsonl>
    python tests/probes/python/_probe_spawn_arg_loss.py <session.jsonl> --raw <model_responses.txt>

`--raw` additionally compares against the provider's recorded tool_use inputs,
which is what lets you say the model itself emitted the short call.

Exit code is always 0: this is a measurement, not an assertion. Read the table.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from pathlib import Path

SPAWN = "spawn_agent"
LOSS_MARK = "<- keys missing"


def iter_session_calls(path: Path):
    """Yield (turn_id, tool_use_id, tool_name, input_dict) for every tool_use in a session log."""
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except Exception:
            continue
        for block in event.get("blocks") or []:
            if block.get("type") == "tool_use":
                yield event.get("turn_id"), block.get("id"), block.get("name"), block.get("input") or {}


def session_summary(path: Path) -> dict:
    all_calls = list(iter_session_calls(path))
    spawns = [c for c in all_calls if c[2] == SPAWN]
    stats = {
        "tool_uses": len(all_calls),
        "spawns": len(spawns),
        "with_raw": sum(1 for _, _, _, i in all_calls if "_raw" in i),
        "split_ids": [i for _, i, _, _ in all_calls if re.search(r"(_\d+|^split_\d+)$", i or "")],
        "key_counts": Counter(tuple(sorted(i)) for _, _, _, i in spawns),
    }
    return stats


def raw_summary(path: Path) -> list[tuple[str, list[str]]]:
    """Pull (timestamp, arg-keys) for every spawn_agent call from a model_responses log."""
    text = path.read_text(encoding="utf-8", errors="replace")
    # Response blocks record the parsed content_blocks as a Python repr.
    blocks = [(m.start(), m.group(2)) for m in re.finditer(r"=== (Prompt|Response) === ([0-9: -]+)", text) if m.group(1) == "Response"]
    rows: list[tuple[str, list[str]]] = []
    for idx, (pos, stamp) in enumerate(blocks):
        end = blocks[idx + 1][0] if idx + 1 < len(blocks) else len(text)
        body = text[pos:end]
        for m in re.finditer(r"'name': 'spawn_agent', 'input': (\{.*?\})\}", body):
            rows.append((stamp, re.findall(r"'(\w+)':", m.group(1))))
    return rows


def truncation_signals(raw_path: Path) -> dict:
    text = raw_path.read_text(encoding="utf-8", errors="replace")
    return {
        "max_tokens": text.count("max_tokens"),
        "content_filter": text.count("content_filter"),
        "stream_broken": text.count("流异常中断"),
        "tool_args_unparsed": 0,  # only ever set by the handler, never by the wire
    }


def report_session(path: Path) -> None:
    stats = session_summary(path)
    print(f"session: {path}")
    print(f"  tool_use blocks : {stats['tool_uses']}")
    print(f"  carrying _raw   : {stats['with_raw']}  (non-zero => transport truncation)")
    print(f"  split-id blocks : {stats['split_ids'] or 'none'}  (non-empty => concatenated JSON)")
    print("  argument-key signatures, most common first:")
    for keys, count in stats["key_counts"].most_common():
        flag = LOSS_MARK if len(keys) <= 1 else ""
        print(f"    {count:3d}x {keys} {flag}")


def report_raw(path: Path) -> None:
    rows = raw_summary(path)
    signals = truncation_signals(path)
    print(f"provider log: {path}")
    print(f"  spawn_agent responses: {len(rows)}")
    print(f"  truncation sentinels : {signals}")
    print("  per call:")
    for stamp, keys in rows:
        missing = "message" not in keys
        print(f"    {stamp}  keys={keys}{'  <- no message' if missing else ''}")
    lone_role = sum(1 for _, keys in rows if keys == ["agent_type"])
    print()
    print(f"  calls with agent_type and nothing else: {lone_role}/{len(rows)}")
    if lone_role:
        print("  => the model emitted a syntactically complete, single-key object.")
        print("     Check the schema's `required` list before blaming the transport.")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Replay a session log to classify spawn_agent argument loss.")
    parser.add_argument("session", type=Path, help="path to session_*.jsonl")
    parser.add_argument("--raw", type=Path, default=None, help="path to temp/model_responses/model_responses_*.txt")
    args = parser.parse_args(argv[1:])

    if not args.session.is_file():
        print(f"no such session log: {args.session}", file=sys.stderr)
        return 2
    report_session(args.session)
    if args.raw:
        if not args.raw.is_file():
            print(f"no such provider log: {args.raw}", file=sys.stderr)
            return 2
        print()
        report_raw(args.raw)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
