"""Move child transcripts that were written before placement changed.

Background
----------
``/resume`` used to list every top-level ``session_*.jsonl`` in the project
bucket, including the transcript every process-backed child (teammate,
subagent) creates for itself. Claude Code never has this problem because agent
transcripts live one level down -- ``<projectDir>/<sessionId>/subagents/
agent-<id>.jsonl`` (m0279 ``lht()``) -- and the picker only enumerates files
(``m0591:33509``: ``if (!S.isFile() || !S.name.endsWith(".jsonl")) continue``).

``transcripts.ensure_agent_session`` now nests new child sessions under
``<project>/<parent>/subagents/sessions/`` and stamps ``agent_kind`` into
``session_start``. This module retires the ones written before that: it moves a
top-level session only when the evidence is exact.

The proof
---------
Every spawned child gets its spawn message written to
``<project>/<parent>/subagents/tasks/<name>/input.txt`` by ``SubagentManager``,
and its own transcript starts with exactly that text. Measured on the real
bucket (2026-11-09): all 109 top-level sessions that look internal match an
``input.txt`` byte for byte, and none of the 21 remaining user sessions matches
any of them. Nothing else is accepted:

* ``agent_kind == "subagent"`` in ``session_start`` (new files);
* an exact ``preview == input.txt`` match naming a single parent.

A prompt that matches more than one parent is reported as ambiguous and left in
place -- moving a real conversation into a teammate's folder is worse than
showing one stale child row.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

from genericagent.core import transcripts as session_transcript


@dataclass(frozen=True)
class ChildSessionMove:
    session_path: str
    parent_session_id: str
    reason: str


@dataclass
class ChildSessionMigrationPlan:
    moves: list[ChildSessionMove] = field(default_factory=list)
    kept: list[str] = field(default_factory=list)
    ambiguous: list[str] = field(default_factory=list)


def _child_prompt_parents(base: Path) -> dict[str, set[str]]:
    """Map every spawn message in this bucket to the parent session(s) that sent it."""
    parents: dict[str, set[str]] = {}
    for path in base.glob("*/subagents/tasks/*/input.txt"):
        try:
            relative = path.relative_to(base)
        except ValueError:
            continue
        if len(relative.parts) < 2:
            continue
        text = ""
        try:
            text = path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
        if text:
            parents.setdefault(text, set()).add(relative.parts[0])
    return parents


def _session_start(path: Path) -> dict:
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            first = fh.readline()
    except OSError:
        return {}
    try:
        event = json.loads(first)
    except Exception:
        return {}
    return event if isinstance(event, dict) and event.get("type") == "session_start" else {}


# A live child keeps appending to its transcript. Moving the file while it runs
# would split it in two: the next append recreates the old path, and the moved
# half stops growing. Anything freshly written is therefore reported, not moved.
DEFAULT_MIN_AGE_SECONDS = 120


def plan_child_session_migration(
    project_data_dir, *, min_age_seconds: float = DEFAULT_MIN_AGE_SECONDS
) -> ChildSessionMigrationPlan:
    """Classify every top-level session in the bucket without touching the filesystem."""
    base = Path(project_data_dir)
    plan = ChildSessionMigrationPlan()
    if not base.exists():
        return plan
    prompts = _child_prompt_parents(base)
    for summary in session_transcript.list_sessions(
        root=base, include_empty=True, include_subagents=True
    ):
        path = Path(summary.path)
        if min_age_seconds and (time.time() - float(summary.mtime or 0)) < min_age_seconds:
            plan.ambiguous.append(str(path))
            continue
        start = _session_start(path)
        if str(start.get("agent_kind") or "").lower() == "subagent":
            parent = str(start.get("parent_session_id") or "").strip()
            if parent:
                plan.moves.append(ChildSessionMove(str(path), parent, "agent_kind"))
            else:
                plan.ambiguous.append(str(path))
            continue
        preview = str(summary.preview or "").strip()
        owners = prompts.get(preview) if preview else None
        if owners:
            if len(owners) == 1:
                plan.moves.append(ChildSessionMove(str(path), next(iter(owners)),
                                                   "task-input-match"))
            else:
                plan.ambiguous.append(str(path))
            continue
        plan.kept.append(str(path))
    return plan


def apply_child_session_migration(plan: ChildSessionMigrationPlan) -> list[dict]:
    """Move each planned file under its parent. Never overwrites, never deletes."""
    applied: list[dict] = []
    for move in plan.moves:
        source = Path(move.session_path)
        target_dir = source.parent / move.parent_session_id / "subagents" / session_transcript.SUBAGENT_SESSIONS_DIRNAME
        target = target_dir / source.name
        record = {
            "session_path": str(source),
            "parent_session_id": move.parent_session_id,
            "reason": move.reason,
            "target": str(target),
            "moved": 0,
        }
        try:
            target_dir.mkdir(parents=True, exist_ok=True)
            if target.exists():
                record["skipped"] = "target exists"
            else:
                os.replace(source, target)
                record["moved"] = 1
        except OSError as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
        applied.append(record)
    return applied


def migrate_child_sessions(project_data_dir, *, apply: bool = False,
                           min_age_seconds: float = DEFAULT_MIN_AGE_SECONDS) -> dict:
    plan = plan_child_session_migration(project_data_dir, min_age_seconds=min_age_seconds)
    result = {
        "project_data_dir": str(project_data_dir),
        "applied": apply,
        "planned": len(plan.moves),
        "kept": len(plan.kept),
        "ambiguous": len(plan.ambiguous),
        "moves": [
            {"session_path": m.session_path, "parent_session_id": m.parent_session_id,
             "reason": m.reason}
            for m in plan.moves
        ],
        "ambiguous_paths": list(plan.ambiguous),
    }
    if apply:
        result["results"] = apply_child_session_migration(plan)
        result["moved"] = sum(1 for item in result["results"] if item.get("moved"))
    return result
