"""Explicit, copy-only migration for GA project-scoped storage.

The migration never deletes a source and never overwrites a target. Legacy
session claims are written to a sidecar JSON file; the original JSONL bytes are
not modified.
"""
from __future__ import annotations

import getpass
import hashlib
import json
import os
import shutil
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from genericagent import paths


@dataclass(frozen=True)
class MigrationItem:
    kind: str
    source: str
    target: str
    status: str
    bytes: int = 0
    sha256: str | None = None
    reason: str | None = None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _directory_sha256(path: Path) -> str:
    """Stable tree digest: relative path plus each file SHA256."""
    digest = hashlib.sha256()
    for child in sorted(path.rglob("*")):
        if not child.is_file():
            continue
        relative = child.relative_to(path).as_posix()
        digest.update(relative.encode("utf-8"))
        digest.update(bytes([10]))
        digest.update(_sha256(child).encode("ascii"))
        digest.update(bytes([10]))
    return digest.hexdigest()


def _tree_item(kind: str, source: Path, target: Path, *, apply: bool) -> list[MigrationItem]:
    """Copy a directory as individual files, never overwriting a target.

    The whole tree is preflighted before the first write.  A conflict in any
    child therefore blocks the tree rather than leaving a partially-copied
    artifact directory behind.
    """
    if not source.is_dir():
        return [MigrationItem(kind, str(source), str(target), "missing")]
    planned: list[MigrationItem] = []
    for child in sorted(source.rglob("*")):
        if child.is_file():
            relative = child.relative_to(source)
            planned.append(_item(kind, child, target / relative, apply=False))
    if any(item.status == "conflict" for item in planned):
        return [
            item if item.status == "conflict" else MigrationItem(
                item.kind,
                item.source,
                item.target,
                "blocked",
                item.bytes,
                item.sha256,
                "blocked by another conflicting file in the same tree",
            )
            for item in planned
        ]
    if not apply:
        return planned
    return [
        _item(item.kind, Path(item.source), Path(item.target), apply=True)
        for item in planned
    ]


def _item(kind: str, source: Path, target: Path, *, apply: bool) -> MigrationItem:
    if not source.is_file():
        return MigrationItem(kind, str(source), str(target), "missing")
    size = source.stat().st_size
    source_hash = _sha256(source)
    if target.exists():
        if target.is_file() and _sha256(target) == source_hash:
            return MigrationItem(kind, str(source), str(target), "already_present", size, source_hash)
        return MigrationItem(kind, str(source), str(target), "conflict", size, source_hash, "target exists with different bytes")
    if not apply:
        return MigrationItem(kind, str(source), str(target), "would_copy", size, source_hash)
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)
    return MigrationItem(kind, str(source), str(target), "copied", size, source_hash)


def migrate_sessions(project_root, data_root, *, legacy_root=None, apply=False) -> list[MigrationItem]:
    roots = paths.project_roots(project=project_root, data_root=data_root)
    source_dir = Path(legacy_root) if legacy_root is not None else roots.legacy_sessions_dir
    items = []
    if source_dir.is_dir():
        for source in sorted(source_dir.glob("session_*.jsonl")):
            items.append(_item("session", source, roots.project_data_dir / source.name, apply=apply))
            session_id = source.stem
            for child_name in ("subagents", "workflows"):
                child = source_dir / session_id / child_name
                if child.is_dir():
                    items.extend(_tree_item(
                        f"session_{child_name}",
                        child,
                        roots.project_data_dir / session_id / child_name,
                        apply=apply,
                    ))
    return items


VALID_NAMED_SCOPES = frozenset({"project", "user"})


def validate_team_key(value: str) -> str:
    """Validate one explicit team-key directory component."""
    text = str(value or "").strip()
    if not text or text in {".", ".."}:
        raise ValueError("team mapping target must be one non-empty directory name")
    if any(part in text for part in ("/", "\\", ":", "\x00")):
        raise ValueError(f"unsafe team mapping target: {text!r}")
    return text


def migrate_named_workflows(project_root, data_root, *, apply=False, scope="project") -> list[MigrationItem]:
    if scope not in VALID_NAMED_SCOPES:
        raise ValueError(f"unknown named workflow scope: {scope}")
    roots = paths.project_roots(project=project_root, data_root=data_root)
    target_root = roots.project_workflows_dir if scope == "project" else roots.user_workflows_dir
    items = []
    if roots.legacy_workflows_dir.is_dir():
        for source in sorted(roots.legacy_workflows_dir.glob("*.json")):
            items.append(_item("named_workflow", source, target_root / source.name, apply=apply))
    return items


def migrate_workflow_runs(project_root, data_root, *, legacy_root=None, apply=False) -> list[MigrationItem]:
    roots = paths.project_roots(project=project_root, data_root=data_root)
    source_dir = Path(legacy_root) if legacy_root is not None else roots.legacy_sessions_dir
    items = []
    if source_dir.is_dir():
        seen_roots: set[Path] = set()
        for source in sorted(source_dir.glob("*/workflows/*/state.json")):
            run_dir = source.parent
            if run_dir in seen_roots:
                continue
            seen_roots.add(run_dir)
            relative = run_dir.relative_to(source_dir)
            items.extend(_tree_item(
                "workflow_run",
                run_dir,
                roots.project_data_dir / relative,
                apply=apply,
            ))
    return items


def _claim_path(source: Path) -> Path:
    return source.with_name(source.name + ".claim.json")


def claim_legacy_session(path, *, project_root, data_root, apply=False) -> MigrationItem:
    source = Path(path)
    if not source.is_file():
        return MigrationItem("legacy_claim", str(source), "", "missing")
    roots = paths.project_roots(project=project_root, data_root=data_root)
    sidecar = _claim_path(source)
    payload = {
        "schema_version": 1,
        "source": str(source),
        "source_sha256": _sha256(source),
        "project_root": str(roots.project_root),
        "project_key": roots.project_key,
        "data_root": str(roots.data_root),
    }
    if sidecar.exists():
        try:
            if json.loads(sidecar.read_text(encoding="utf-8")) == payload:
                return MigrationItem("legacy_claim", str(source), str(sidecar), "already_present")
        except Exception:
            return MigrationItem("legacy_claim", str(source), str(sidecar), "conflict", reason="invalid existing claim sidecar")
    if not apply:
        return MigrationItem("legacy_claim", str(source), str(sidecar), "would_claim")
    sidecar.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8", newline="\n")
    return MigrationItem("legacy_claim", str(source), str(sidecar), "claimed")


def migrate_team_tasks(project_root, data_root, *, team_map=None, apply=False) -> list[MigrationItem]:
    """Copy legacy team boards only when the operator supplies project mapping.

    Legacy ``temp/teams/<listId>`` has no trustworthy project identity. The
    mapping value is the full new team key, normally
    ``<projectKey>--<teamName>``. Unmapped boards are returned as ``skipped``
    and are never guessed or modified.
    """
    roots = paths.project_roots(project=project_root, data_root=data_root)
    source_root = roots.data_root / "temp" / "teams"
    mapping = {str(key): validate_team_key(value) for key, value in (team_map or {}).items()}
    items: list[MigrationItem] = []
    if not source_root.is_dir():
        return items
    for source in sorted(source_root.iterdir()):
        if not source.is_dir():
            continue
        mapped = mapping.get(source.name)
        if mapped is None:
            items.append(MigrationItem(
                "team_task",
                str(source),
                "",
                "skipped",
                reason="no explicit --team-map for legacy team board; project ownership is unknown",
            ))
            continue
        items.extend(_tree_item(
            "team_task",
            source,
            roots.tasks_dir / mapped,
            apply=apply,
        ))
    return items


def _write_report(project_root, data_root, report: dict) -> Path:
    roots = paths.project_roots(project=project_root, data_root=data_root)
    report_dir = roots.data_root / "migrations"
    report_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    path = report_dir / f"paths-{stamp}.json"
    handle = open(path, "w", encoding="utf-8")
    with handle:
        handle.write(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
        handle.write(chr(10))
    return path


def _safe_operator() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return "unknown"


def _batch_signature(roots, entries) -> str:
    digest = hashlib.sha256()
    for kind, source, target in sorted(entries):
        for value in (kind, source, target):
            digest.update(str(value).encode("utf-8"))
            digest.update(bytes([10]))
    return digest.hexdigest()


def _batch_ledger_path(roots, signature: str) -> Path:
    return roots.data_root / "migrations" / "batches" / f"{signature}.json"


def _read_batch_ledger(path: Path):
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None
    return value if isinstance(value, dict) else None


def _copy_selected(project_root, data_root, kinds, team_map):
    items: list[MigrationItem] = []
    if "sessions" in kinds:
        items.extend(migrate_sessions(project_root, data_root, apply=True))
    if "workflows" in kinds:
        items.extend(migrate_workflow_runs(project_root, data_root, apply=True))
    if "named" in kinds:
        items.extend(migrate_named_workflows(project_root, data_root, apply=True))
    if "teams" in kinds:
        items.extend(migrate_team_tasks(project_root, data_root, team_map=team_map, apply=True))
    return items


def _summary(items) -> dict:
    statuses = sorted({item.status for item in items})
    return {status: sum(1 for item in items if item.status == status) for status in statuses}


def run_migration(
    project_root,
    data_root,
    *,
    apply=False,
    kinds=None,
    report=False,
    team_map=None,
    operator=None,
) -> dict:
    kinds = set(kinds or {"sessions", "workflows", "named"})
    unknown = kinds - {"sessions", "workflows", "named", "teams"}
    if unknown:
        raise ValueError(f"unknown migration kinds: {', '.join(sorted(unknown))}")
    team_map = dict(team_map or {})
    for value in team_map.values():
        validate_team_key(value)

    roots = paths.project_roots(project=project_root, data_root=data_root)
    preview: list[MigrationItem] = []
    if "sessions" in kinds:
        preview.extend(migrate_sessions(project_root, data_root, apply=False))
    if "workflows" in kinds:
        preview.extend(migrate_workflow_runs(project_root, data_root, apply=False))
    if "named" in kinds:
        preview.extend(migrate_named_workflows(project_root, data_root, apply=False))
    if "teams" in kinds:
        preview.extend(migrate_team_tasks(project_root, data_root, team_map=team_map, apply=False))

    entries = [(item.kind, item.source, item.target) for item in preview if item.target]
    signature = _batch_signature(roots, entries)
    ledger_path = _batch_ledger_path(roots, signature)
    prior = _read_batch_ledger(ledger_path)
    conflicts = [item for item in preview if item.status == "conflict"]
    items = preview
    apply_blocked = bool(apply and conflicts)

    if apply and not conflicts:
        items = _copy_selected(project_root, data_root, kinds, team_map)
        conflicts = [item for item in items if item.status == "conflict"]
        apply_blocked = bool(conflicts)
        if not conflicts:
            ledger_path.parent.mkdir(parents=True, exist_ok=True)
            ledger = {
                "schema_version": 1,
                "applied": True,
                "timestamp_utc": datetime.now(timezone.utc).isoformat(),
                "operator": str(operator or _safe_operator()),
                "signature": signature,
                "sources": [{"kind": k, "path": s} for k, s, _ in entries],
                "targets": [{"kind": k, "path": t} for k, _, t in entries],
            }
            ledger_path.write_text(
                json.dumps(ledger, ensure_ascii=False, indent=2, sort_keys=True) + chr(10),
                encoding="utf-8",
                newline=chr(10),
            )

    result = {
        "schema_version": 1,
        "timestamp_utc": datetime.now(timezone.utc).isoformat(),
        "operator": str(operator or _safe_operator()),
        "mode": "apply" if apply else "dry-run",
        "apply": bool(apply),
        "project_root": str(roots.project_root),
        "project_key": roots.project_key,
        "data_root": str(roots.data_root),
        "kinds": sorted(kinds),
        "team_map": dict(sorted(team_map.items())),
        "signature": signature,
        "items": [asdict(item) for item in items],
        "conflicts": [asdict(item) for item in conflicts],
        "summary": _summary(items),
    }
    if prior and prior.get("applied") is True:
        result["previous_apply"] = prior
    if apply:
        result["apply_blocked"] = apply_blocked
        if apply_blocked:
            result["apply_block_reason"] = "resolve all conflicts before applying; no files were copied"
        if not apply_blocked:
            result["ledger_path"] = str(ledger_path)
    if report:
        result["report_path"] = str(_write_report(project_root, data_root, result))
    return result
