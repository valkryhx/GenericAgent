"""Central project/resource path resolution for GenericAgent.

The package may be executed from the repository, from a worktree, or from a
temporary cwd changed by ``code_run``.  Business modules must not guess the
project root from their own ``__file__`` location.
"""
from __future__ import annotations

import os
from pathlib import Path


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

    # Development fallback: if the package is installed in an unusual layout,
    # keep the package parent rather than silently writing into site-packages.
    return package_root.parent.resolve()


PACKAGE_ROOT = Path(__file__).resolve().parent
PROJECT_ROOT = _discover_project_root()
TEMP_DIR = PROJECT_ROOT / "temp"
ASSETS_DIR = PROJECT_ROOT / "assets"
SKILLS_DIR = PROJECT_ROOT / "skills"
MEMORY_DIR = PROJECT_ROOT / "memory"
FRONTENDS_DIR = PROJECT_ROOT / "frontends"
SESSIONS_DIR = TEMP_DIR / "sessions"
RESOURCES_DIR = PACKAGE_ROOT / "resources"
WORKFLOW_JS_DIR = PACKAGE_ROOT / "workflow" / "js"

MCP_CONFIG_PATH = Path(os.environ.get("GA_MCP_CONFIG") or (PROJECT_ROOT / "mcp.json"))
LLM_CONFIG_PATH = Path(os.environ.get("GA_LLM_CONFIG") or (PROJECT_ROOT / "llm.yaml"))


def ensure_runtime_dirs() -> None:
    """Create writable runtime directories used by the core runtime."""
    for path in (TEMP_DIR, TEMP_DIR / "model_responses", SESSIONS_DIR):
        path.mkdir(parents=True, exist_ok=True)
