"""Central project/resource path resolution for GenericAgent.

The package may be executed from the repository, from a worktree, or from a
temporary cwd changed by ``code_run``.  Business modules must not guess the
project root from their own ``__file__`` location.
"""
from __future__ import annotations

import os
import shutil
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
