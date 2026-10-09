"""Canonical workspace and artifact-path enforcement for workflows."""

from __future__ import annotations

import os
from pathlib import Path


class WorkspacePathError(ValueError):
    """Raised when a path cannot be safely contained by a workspace."""


def default_workspace_root() -> Path:
    """The directory workflow artifacts are rooted at.

    Product decision (2026-10-03): artifacts must not land next to GA's own
    source. ``os.getcwd()`` did exactly that for the normal ``ga`` launch, which
    starts in the repository root, so a finished report showed up beside
    ``agentmain.py``. The default root is the repository ``temp/`` directory --
    already the project's scratch-output location and already gitignored -- and
    children may create subdirectories under it (or write a file directly in
    it). ``GA_WORKFLOW_WORKSPACE_ROOT`` overrides the location for tests and
    embedding; an explicitly supplied workspace still wins over both.
    """

    override = os.environ.get("GA_WORKFLOW_WORKSPACE_ROOT")
    if override and str(override).strip():
        return Path(str(override).strip()).expanduser()
    project_dir = Path(__file__).resolve().parent
    return project_dir / "temp"


def resolve_workspace_root(raw: str | os.PathLike[str] | None = None) -> Path:
    candidate = Path(raw).expanduser() if raw is not None else default_workspace_root()
    try:
        root = candidate.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorkspacePathError(f"workspace is not a resolvable directory: {candidate}") from exc
    if not root.is_dir():
        raise WorkspacePathError(f"workspace must be an existing directory: {root}")
    return root


RUN_WORKSPACE_DIRNAME = "workflow-runs"


def run_workspace_path(base_root: str | os.PathLike[str], run_id: str) -> Path:
    """Return the per-run workspace path without creating it.

    Two concurrent workflow runs previously shared ``temp/`` and overwrote each
    other's artifacts. Each run now owns ``<base>/workflow-runs/<run_id>/`` so
    its deliverables are isolated, still under the gitignored project temp root,
    and still inspectable by a human after the run.
    """
    base = resolve_workspace_root(base_root)
    run_id = str(run_id or "").strip()
    if not run_id or run_id in {".", ".."} or "/" in run_id or "\\" in run_id:
        raise WorkspacePathError(f"invalid run id for workspace: {run_id!r}")
    return (base / RUN_WORKSPACE_DIRNAME / run_id).resolve()


def create_run_workspace(base_root: str | os.PathLike[str], run_id: str) -> Path:
    root = run_workspace_path(base_root, run_id)
    root.mkdir(parents=True, exist_ok=True)
    return root


def snapshot_workspace(root: str | os.PathLike[str], *, max_entries: int = 20_000) -> dict[str, tuple[int, int]]:
    """Map workspace-relative file paths to ``(mtime_ns, size)``.

    Used to detect what a child produced without enumerating tool names: any
    tool that writes (``file_write``, ``code_run``, or a future one) changes the
    filesystem, so the before/after difference is the ground truth.

    ``os.scandir`` is used instead of ``os.walk`` because the ancestor
    directories are rarely huge but the workspace can be a full checkout; the
    scan runs twice per child job, and ``os.walk`` re-stats every entry through
    string paths (1.7s on this repository versus 0.07s here).
    """
    base = Path(root)
    if not base.is_dir():
        return {}
    snapshot: dict[str, tuple[int, int]] = {}
    pending = [str(base)]
    while pending:
        directory = pending.pop()
        try:
            entries = os.scandir(directory)
        except OSError:
            continue
        with entries:
            for entry in entries:
                try:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name not in {"__pycache__", ".git"}:
                            pending.append(entry.path)
                        continue
                    if not entry.is_file(follow_symlinks=False):
                        continue
                    stat = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                relative = os.path.relpath(entry.path, base).replace(os.sep, "/")
                snapshot[relative] = (int(stat.st_mtime_ns), int(stat.st_size))
                if len(snapshot) >= max_entries:
                    return snapshot
    return snapshot


def diff_workspace(
    before: dict[str, tuple[int, int]] | None,
    after: dict[str, tuple[int, int]] | None,
) -> list[str]:
    """Return workspace-relative paths created or modified between snapshots."""
    if not after:
        return []
    if not before:
        return sorted(after)
    changed = [relative for relative, signature in after.items() if before.get(relative) != signature]
    return sorted(changed)



def workspace_writes_with_writer(entries) -> list[dict[str, str]]:
    """Normalize observed-write entries to ``[{"path", "writer"}]``.

    ``observedArtifacts`` started as a bare list of workspace-relative paths.
    That is enough to hand a file to the next agent, but not to answer "which
    job produced this file" when two children write the same path. The writer is
    recorded at the same moment as the diff -- the only place that knows the job
    -- instead of being inferred later from tool names, which is exactly the
    enumeration this module was changed to avoid.

    Accepts both shapes so old run state keeps loading: bare strings become
    entries with an empty writer, and dict entries keep their ``path``/``writer``.
    """
    normalized: list[dict[str, str]] = []
    for entry in entries or []:
        if isinstance(entry, dict):
            path = str(entry.get("path") or "").strip()
            if not path:
                continue
            normalized.append({"path": path, "writer": str(entry.get("writer") or "").strip()})
            continue
        path = str(entry or "").strip()
        if path:
            normalized.append({"path": path, "writer": ""})
    return normalized


def observed_artifact_paths(entries) -> list[str]:
    """Return just the paths from an observed-artifact list (either shape)."""
    return [entry["path"] for entry in workspace_writes_with_writer(entries)]


def observed_artifact_owners(entries) -> dict[str, list[str]]:
    """Map a path to the writer labels that produced it, for collision reporting."""
    owners: dict[str, list[str]] = {}
    for entry in workspace_writes_with_writer(entries):
        writer = entry["writer"]
        if not writer:
            continue
        bucket = owners.setdefault(entry["path"], [])
        if writer not in bucket:
            bucket.append(writer)
    return owners

def _is_absolute_like(raw: str) -> bool:
    value = str(raw).strip().replace("\\", "/")
    return bool(value.startswith("/") or value.startswith("//") or (len(value) >= 2 and value[1] == ":"))


def normalize_workspace_relative(raw: str | os.PathLike[str], root: Path, *, allow_in_root_absolute: bool = True) -> str:
    """Return one POSIX relative path; reject outside absolute/traversal paths."""
    if raw is None or not str(raw).strip():
        raise WorkspacePathError("workspace path must be non-empty")
    text = str(raw).strip().replace("\\", "/")
    workspace = resolve_workspace_root(root)
    candidate = Path(text)
    if _is_absolute_like(text):
        if not allow_in_root_absolute:
            raise WorkspacePathError(f"absolute path is not allowed: {text}")
        try:
            absolute = candidate.resolve(strict=False)
            relative = absolute.relative_to(workspace)
        except (OSError, RuntimeError, ValueError) as exc:
            raise WorkspacePathError(f"path is outside workspace: {text}") from exc
    else:
        if candidate.anchor or ".." in candidate.parts:
            raise WorkspacePathError(f"path must stay within workspace: {text}")
        relative = candidate
    # Only strip an explicit "./" prefix. Using ``lstrip("./")`` here also
    # removed the leading dot of a legitimate dotfile artifact such as
    # ``.report.html``, which made host artifact checks look for the wrong
    # path even though the child wrote the file correctly.
    normalized = relative.as_posix()
    if normalized.startswith("./"):
        normalized = normalized[2:]
    if not normalized or normalized in {".", ".."} or normalized.startswith("../"):
        raise WorkspacePathError(f"path must name a workspace child: {text}")
    return normalized


def normalize_declared_artifact_path(raw: str | os.PathLike[str], root: Path) -> str:
    """Normalize a planner-declared artifact path without granting host access.

    Legacy planners often emit ``/tmp/name`` while intending the conventional
    ``tmp/name`` folder under the user workspace.  That one known prefix is
    converted deterministically; every other outside absolute path is rejected.
    This function is for plan normalization only.  Tool execution still uses
    ``normalize_workspace_relative`` and therefore rejects outside absolutes.
    """
    text = str(raw or "").strip().replace("\\", "/")
    if text.startswith("/tmp/") or text.startswith("/tmp") and text != "/tmp":
        suffix = text[len("/tmp"):].lstrip("/")
        return normalize_workspace_relative(f"tmp/{suffix}", root, allow_in_root_absolute=False)
    if len(text) >= 2 and text[1] == ":":
        drive_tail = text[2:].lstrip("/")
        if drive_tail.lower().startswith("tmp/"):
            return normalize_workspace_relative(drive_tail, root, allow_in_root_absolute=False)
    return normalize_workspace_relative(text, root)


def resolve_workspace_child(raw: str | os.PathLike[str], root: Path) -> Path:
    workspace = resolve_workspace_root(root)
    if str(raw or "").strip().replace("\\", "/") in {".", "./"}:
        return workspace
    relative = normalize_workspace_relative(raw, workspace)
    target = (workspace / relative).resolve(strict=False)
    try:
        target.relative_to(workspace)
    except ValueError as exc:
        raise WorkspacePathError(f"path is outside workspace: {raw}") from exc
    return target


def workspace_metadata(root: Path) -> dict[str, str]:
    workspace = resolve_workspace_root(root)
    return {"workspacePath": str(workspace), "workspacePolicy": "project-temp-workspace-write-v1"}
