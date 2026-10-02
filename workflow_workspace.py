"""Canonical workspace and artifact-path enforcement for workflows."""

from __future__ import annotations

import os
from pathlib import Path


class WorkspacePathError(ValueError):
    """Raised when a path cannot be safely contained by a workspace."""


def resolve_workspace_root(raw: str | os.PathLike[str] | None = None) -> Path:
    candidate = Path(raw if raw is not None else os.getcwd()).expanduser()
    try:
        root = candidate.resolve(strict=True)
    except (OSError, RuntimeError, ValueError) as exc:
        raise WorkspacePathError(f"workspace is not a resolvable directory: {candidate}") from exc
    if not root.is_dir():
        raise WorkspacePathError(f"workspace must be an existing directory: {root}")
    return root


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
    normalized = relative.as_posix().lstrip("./")
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
    return {"workspacePath": str(workspace), "workspacePolicy": "cwd-rooted-workspace-write-v1"}
