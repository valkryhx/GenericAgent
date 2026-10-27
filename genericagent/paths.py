"""Central project/resource path resolution for GenericAgent.

The package may be executed from the repository, from a worktree, or from a
temporary cwd changed by ``code_run``.  Business modules must not guess the
project root from their own ``__file__`` location.
"""
from __future__ import annotations

import hashlib
import os
import re
import shutil
import unicodedata
from dataclasses import dataclass
from pathlib import Path


def user_data_dir() -> Path:
    """Return the per-user writable runtime directory.

    Priority:
      1. GA_DATA_DIR
      2. Windows: %LOCALAPPDATA%/GenericAgent
      3. macOS/Linux: $XDG_DATA_HOME/genericagent or ~/.local/share/genericagent
    """
    override = os.environ.get("GA_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or os.environ.get("USERPROFILE") or str(Path.home())
        return (Path(base) / "GenericAgent").resolve()
    xdg = os.environ.get("XDG_DATA_HOME")
    return ((Path(xdg) if xdg else Path.home() / ".local" / "share") / "genericagent").resolve()


def _discover_project_root() -> Path:
    override = os.environ.get("GA_PROJECT_ROOT")
    if override:
        return Path(override).expanduser().resolve()

    package_root = Path(__file__).resolve().parent
    candidates = [package_root.parent, package_root]
    for candidate in candidates:
        if (
            (candidate / "pyproject.toml").is_file()
            or (candidate / "ga").is_file()
            or (candidate / "assets").is_dir()
        ):
            return candidate.resolve()

    # A wheel install must never write temp/session/memory files into
    # site-packages.  Installed resources live under genericagent/resources;
    # mutable runtime state goes to the per-user data directory instead.
    return user_data_dir().resolve()


PACKAGE_ROOT = Path(__file__).resolve().parent
# Installation shape is a property of where *this package* lives, not of the
# caller's workspace.  A user project may legitimately contain its own
# pyproject.toml; that must never turn an installed wheel into a source checkout
# or redirect mutable state into the project.
def is_source_checkout() -> bool:
    """Whether this package is being imported from a source checkout."""
    return (PACKAGE_ROOT.parent / "pyproject.toml").is_file()


def runtime_root() -> Path:
    """Return the writable root for GA-owned runtime state."""
    return PROJECT_ROOT if is_source_checkout() else user_data_dir()


SOURCE_CHECKOUT = is_source_checkout()
PROJECT_ROOT = _discover_project_root()
# Runtime state is writable user state, not the user's project checkout.  In a
# source checkout these paths intentionally remain at the repository root for
# backward compatibility; in an installed wheel they live under GA_DATA_DIR.
RUNTIME_ROOT = runtime_root()
TEMP_DIR = RUNTIME_ROOT / "temp"
ASSETS_DIR = RUNTIME_ROOT / "assets"
SKILLS_DIR = RUNTIME_ROOT / "skills"
MEMORY_DIR = RUNTIME_ROOT / "memory"
RESOURCES_DIR = PACKAGE_ROOT / "resources"
BUNDLED_ASSETS_DIR = RESOURCES_DIR / "assets"
BUNDLED_SKILLS_DIR = RESOURCES_DIR / "skills"
_INSTALLED_FRONTENDS_DIR = PACKAGE_ROOT.parent / "frontends"
FRONTENDS_DIR = _INSTALLED_FRONTENDS_DIR if _INSTALLED_FRONTENDS_DIR.is_dir() else PROJECT_ROOT / "frontends"
SESSIONS_DIR = TEMP_DIR / "sessions"
WORKFLOW_JS_DIR = PACKAGE_ROOT / "workflow" / "js"

MCP_CONFIG_PATH = Path(os.environ.get("GA_MCP_CONFIG") or (PROJECT_ROOT / "mcp.json"))


def user_config_dir() -> Path:
    """Return the per-user writable config directory.

    Priority:
      1. GA_CONFIG_DIR
      2. Windows: %APPDATA%/GenericAgent
      3. macOS/Linux: $XDG_CONFIG_HOME/genericagent or ~/.config/genericagent
    """
    override = os.environ.get("GA_CONFIG_DIR")
    if override:
        return Path(override).expanduser().resolve()
    if os.name == "nt":
        base = os.environ.get("APPDATA") or os.environ.get("USERPROFILE") or str(Path.home())
        return (Path(base) / "GenericAgent").resolve()
    xdg = os.environ.get("XDG_CONFIG_HOME")
    return ((Path(xdg) if xdg else Path.home() / ".config") / "genericagent").resolve()


def llm_config_candidates() -> list[Path]:
    """Return llm.yaml lookup candidates, highest priority first.

    GA_LLM_CONFIG is an explicit file override.  A source checkout preserves
    the historical project-local config lookup; an installed wheel always
    prefers the per-user config so upgrading it cannot overwrite user settings.
    """
    override = os.environ.get("GA_LLM_CONFIG")
    if override:
        return [Path(override).expanduser().resolve()]

    bases = [user_config_dir()]
    if is_source_checkout():
        bases.insert(0, PROJECT_ROOT)
    candidates: list[Path] = []
    for base in bases:
        for name in ("llm.yaml", "llm.yml"):
            path = base / name
            if path not in candidates:
                candidates.append(path)
    return candidates


def resolve_llm_config_path() -> Path | None:
    """Return the first existing llm.yaml, or None."""
    for path in llm_config_candidates():
        if path.is_file():
            return path
    return None


def llm_template_path() -> Path | None:
    """Return the read-only llm.yaml example shipped with source or wheel."""
    # Bundled resources are immutable and always present in a wheel.  In a
    # source checkout, prefer the root template so local edits are visible.
    sources = [BUNDLED_ASSETS_DIR.parent / "llm.yaml.example"]
    if is_source_checkout():
        sources.insert(0, PROJECT_ROOT / "llm.yaml.example")
    for source in sources:
        if source.is_file():
            return source
    return None


def seed_llm_config() -> Path | None:
    """Copy llm.yaml.example into the user config dir on first run.

    Never overwrites an existing config.  Returns the destination path when a
    new file was created, otherwise None.
    """
    target_dir = user_config_dir()
    target = target_dir / "llm.yaml"
    if target.exists():
        return None
    source = llm_template_path()
    if source is None:
        return None
    target_dir.mkdir(parents=True, exist_ok=True)
    target.write_bytes(source.read_bytes())
    return target


LLM_CONFIG_PATH = resolve_llm_config_path() or (user_config_dir() / "llm.yaml")


def _copy_tree_missing(source: Path, target: Path) -> None:
    """Copy bundled read-only resources into the writable runtime directory.

    Existing user files are never overwritten.  This is a merge, not a mirror.
    """
    if not source.is_dir():
        return
    try:
        target.mkdir(parents=True, exist_ok=True)
        for item in source.rglob("*"):
            if not item.is_file():
                continue
            relative = item.relative_to(source)
            destination = target / relative
            if destination.exists():
                continue
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, destination)
    except OSError:
        # Runtime asset seeding is best-effort; callers report the concrete
        # failure when a required feature asks for a missing file.
        return


def ensure_runtime_assets() -> None:
    """Seed assets and bundled skills into the per-user runtime directory."""
    _copy_tree_missing(BUNDLED_ASSETS_DIR, ASSETS_DIR)
    _copy_tree_missing(BUNDLED_SKILLS_DIR, SKILLS_DIR)


def ensure_runtime_dirs() -> None:
    """Create writable runtime directories used by the core runtime."""
    for path in (TEMP_DIR, TEMP_DIR / "model_responses", SESSIONS_DIR):
        path.mkdir(parents=True, exist_ok=True)
    ensure_runtime_assets()
_PROJECT_ROOT_MARKERS = (".git", ".ga", "pyproject.toml", "GA_AGENTS.md")
_WINDOWS_DEVICE_NAMES = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}


def _resolved_project_path(path: str | os.PathLike[str]) -> Path:
    return Path(unicodedata.normalize("NFC", os.fspath(path))).expanduser().resolve(strict=False)


def _normalized_path(path: str | os.PathLike[str]) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _project_boundary(path: Path) -> Path:
    current = path.parent if path.is_file() else path
    for candidate in (current, *current.parents):
        if any((candidate / marker).exists() for marker in _PROJECT_ROOT_MARKERS):
            return candidate
    return current


def _project_key_identity(path: Path) -> str:
    text = unicodedata.normalize("NFC", str(path))
    return text.casefold() if os.name == "nt" or re.match(r"^[A-Za-z]:[\\/]", text) else text


def _key_for_boundary(boundary: Path) -> str:
    display_name = unicodedata.normalize("NFC", boundary.name)
    if os.name == "nt" or re.match(r"^[A-Za-z]:[\\/]", str(boundary)):
        display_name = display_name.casefold()
    readable = re.sub(r"[^A-Za-z0-9_-]+", "-", display_name).strip("-_")[:48] or "project"
    if readable.upper() in _WINDOWS_DEVICE_NAMES:
        readable = f"project-{readable}"
    digest = hashlib.sha256(_project_key_identity(boundary).encode("utf-8")).hexdigest()[:16]
    return f"{readable}-{digest}"


def project_key(path: str | os.PathLike[str] | None = None) -> str:
    """Return a key after discovering the project boundary from *path*.

    This is the caller-facing rule for a launch subdirectory: ``src/deep``
    inside a Git/GA project maps to that project. It must not be used for a
    path already supplied as an explicit project root.
    """
    boundary = _project_boundary(_resolved_project_path(path if path is not None else os.getcwd()))
    return _key_for_boundary(boundary)


def project_key_exact(path: str | os.PathLike[str]) -> str:
    """Return the key for *path* itself, never an ancestor.

    Environment-provided ``GA_PROJECT_ROOT`` and resolved runtime roots are
    already authoritative. A blank directory is a valid project even when its
    parent happens to contain ``.git``; walking upward would make it silently
    share the parent repository's data bucket.
    """
    return _key_for_boundary(_resolved_project_path(path))


def sanitize_scope_component(value: str, *, fallback: str = "default") -> str:
    """Return a stable filesystem component for a project/team identity."""
    text = unicodedata.normalize("NFC", str(value or "")).strip()
    text = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-._")
    if not text:
        text = fallback
    if text.upper() in _WINDOWS_DEVICE_NAMES:
        text = f"{fallback}-{text}"
    return text[:96]


def team_key(project_key_value: str, team_name: str) -> str:
    """Return the global team identity: ``<projectKey>--<teamName>``."""
    project = sanitize_scope_component(project_key_value, fallback="project")
    team = sanitize_scope_component(team_name, fallback="default")
    return f"{project}--{team}"


@dataclass(frozen=True)
class ProjectRoots:
    project_root: Path
    data_root: Path
    project_key: str
    project_data_dir: Path
    project_roles_dir: Path
    project_workflows_dir: Path
    project_worktrees_dir: Path
    user_workflows_dir: Path
    legacy_workflows_dir: Path
    teams_dir: Path
    tasks_dir: Path
    legacy_sessions_dir: Path


@dataclass(frozen=True)
class RuntimePaths:
    package_root: Path
    original_cwd: Path
    project_root: Path
    launch_cwd: Path
    project_key: str
    data_root: Path
    project_data_dir: Path
    session_data_dir: Path | None
    tool_cwd: Path
    worktree_root: Path
    project_workflows_dir: Path
    user_workflows_dir: Path
    legacy_workflows_dir: Path
    session_transcript: Path | None = None
    logs_dir: Path | None = None
    media_dir: Path | None = None
    mcp_state_dir: Path | None = None

    def __post_init__(self):
        if self.project_key != project_key_exact(self.project_root):
            raise ValueError(f"project_identity_mismatch: project_key={self.project_key!r}")
        expected_data = self.data_root / "projects" / self.project_key
        if _normalized_path(self.project_data_dir) != _normalized_path(expected_data):
            raise ValueError(f"project_identity_mismatch: project_data_dir={self.project_data_dir}")
        expected_workflows = self.project_root / ".ga" / "workflows"
        if _normalized_path(self.project_workflows_dir) != _normalized_path(expected_workflows):
            raise ValueError(f"project_identity_mismatch: project_workflows_dir={self.project_workflows_dir}")


def project_sessions_dir(*, roots=None) -> Path:
    roots = roots or project_roots()
    return roots.project_data_dir


def project_workflow_runs_dir(*, roots=None) -> Path:
    roots = roots or project_roots()
    return roots.project_data_dir / "workflow-runs"


def project_workflow_children_dir(*, roots=None) -> Path:
    roots = roots or project_roots()
    return roots.project_data_dir / "workflow_child_agents"


def project_subagents_dir(session_id: str, *, roots=None) -> Path:
    roots = roots or project_roots()
    return roots.project_data_dir / str(session_id) / "subagents"


def project_workflow_artifacts_dir(session_id: str, *, roots=None) -> Path:
    roots = roots or project_roots()
    return roots.project_data_dir / str(session_id) / "workflows"


def team_dir(project_key_value: str, team_name: str, *, roots=None) -> Path:
    roots = roots or project_roots()
    return roots.teams_dir / team_key(project_key_value, team_name)


def task_dir(project_key_value: str, team_name: str, *, roots=None) -> Path:
    roots = roots or project_roots()
    return roots.tasks_dir / team_key(project_key_value, team_name)


def legacy_sessions_dir(*, roots=None) -> Path:
    roots = roots or project_roots()
    return roots.legacy_sessions_dir


def launch_cwd() -> Path:
    override = os.environ.get("GA_LAUNCH_CWD")
    return _normalized_path(override) if override else Path.cwd().resolve(strict=False)


def project_dir(path=None) -> Path:
    if path is not None:
        return _resolved_project_path(path)
    override = os.environ.get("GA_PROJECT_ROOT")
    return _resolved_project_path(override) if override else _project_boundary(Path.cwd().resolve(strict=False))


def data_root() -> Path:
    return user_data_dir()


def project_roots(*, project=None, data_root=None) -> ProjectRoots:
    root = _normalized_path(project_dir(project))
    data = _normalized_path(data_root if data_root is not None else globals()["data_root"]())
    key = project_key_exact(root)
    return ProjectRoots(
        project_root=root,
        data_root=data,
        project_key=key,
        project_data_dir=data / "projects" / key,
        project_roles_dir=root / ".ga" / "subagents",
        project_workflows_dir=root / ".ga" / "workflows",
        project_worktrees_dir=root / ".ga" / "worktrees",
        user_workflows_dir=data / "workflows",
        legacy_workflows_dir=data / "temp" / "sessions" / "named-workflows",
        teams_dir=data / "teams",
        tasks_dir=data / "tasks",
        legacy_sessions_dir=data / "temp" / "sessions",
    )


def runtime_paths(*, roots=None, session_id=None, launch=None, original_cwd=None, tool_cwd=None) -> RuntimePaths:
    roots = roots or project_roots()
    sid = str(session_id or "").strip()
    return RuntimePaths(
        package_root=PACKAGE_ROOT,
        original_cwd=_normalized_path(original_cwd or roots.project_root),
        project_root=roots.project_root,
        launch_cwd=_normalized_path(launch or roots.project_root),
        project_key=roots.project_key,
        data_root=roots.data_root,
        project_data_dir=roots.project_data_dir,
        session_data_dir=roots.project_data_dir / sid if sid else None,
        tool_cwd=_normalized_path(tool_cwd or roots.project_root),
        worktree_root=roots.project_worktrees_dir,
        project_workflows_dir=roots.project_workflows_dir,
        user_workflows_dir=roots.user_workflows_dir,
        legacy_workflows_dir=roots.legacy_workflows_dir,
        session_transcript=roots.project_data_dir / f"{sid}.jsonl" if sid else None,
        logs_dir=roots.data_root / "logs",
        media_dir=roots.data_root / "media",
        mcp_state_dir=roots.data_root / "mcp",
    )
