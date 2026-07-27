import os
from dataclasses import dataclass
from pathlib import Path


DEFAULT_GA_AGENTS_FILENAME = "GA_AGENTS.md"
LOCAL_GA_AGENTS_FILENAME = "GA_AGENTS.override.md"
DEFAULT_PROJECT_DOC_MAX_BYTES = 20000


@dataclass(frozen=True)
class LoadedGaAgentsDoc:
    path: Path
    rel_path: str
    content: str
    truncated: bool = False


@dataclass(frozen=True)
class GaProjectInstructions:
    docs: tuple[LoadedGaAgentsDoc, ...]
    max_bytes: int
    truncated: bool = False


def _resolve(path) -> Path:
    return Path(path).expanduser().resolve(strict=False)


def _max_bytes(value=None) -> int:
    if value is None:
        value = os.environ.get("GA_PROJECT_DOC_MAX_BYTES", DEFAULT_PROJECT_DOC_MAX_BYTES)
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return DEFAULT_PROJECT_DOC_MAX_BYTES


def _current_within_root(workspace_root: Path, current_dir=None) -> Path:
    root = _resolve(workspace_root)
    current = _resolve(current_dir or root)
    try:
        current.relative_to(root)
    except ValueError:
        return root
    if current.is_file():
        return current.parent
    return current


def _search_dirs(workspace_root: Path, current_dir: Path) -> list[Path]:
    root = _resolve(workspace_root)
    current = _current_within_root(root, current_dir)
    dirs = []
    cursor = current
    while True:
        dirs.append(cursor)
        if cursor == root:
            break
        parent = cursor.parent
        if parent == cursor:
            break
        cursor = parent
    dirs.reverse()
    return dirs


def discover_ga_agents_paths(workspace_root, current_dir=None) -> list[Path]:
    root = _resolve(workspace_root)
    current = _current_within_root(root, current_dir)
    paths: list[Path] = []
    candidates = (LOCAL_GA_AGENTS_FILENAME, DEFAULT_GA_AGENTS_FILENAME)
    for directory in _search_dirs(root, current):
        for name in candidates:
            candidate = directory / name
            if candidate.is_file():
                paths.append(candidate)
                break
    return paths


def load_ga_project_instructions(workspace_root=None, current_dir=None, max_bytes=None) -> GaProjectInstructions:
    root = _resolve(workspace_root or Path(__file__).resolve().parent)
    budget = _max_bytes(max_bytes)
    if budget == 0:
        return GaProjectInstructions(docs=(), max_bytes=0, truncated=False)

    paths = discover_ga_agents_paths(root, current_dir or root)
    remaining = budget
    docs: list[LoadedGaAgentsDoc] = []
    truncated_any = False

    for index, path in enumerate(paths):
        if remaining <= 0:
            truncated_any = index < len(paths)
            break
        try:
            data = path.read_bytes()
        except OSError:
            continue
        truncated = False
        if len(data) > remaining:
            data = data[:remaining]
            truncated = True
            truncated_any = True
        text = data.decode("utf-8", errors="replace")
        if text.strip():
            try:
                rel_path = str(path.relative_to(root))
            except ValueError:
                rel_path = str(path)
            docs.append(LoadedGaAgentsDoc(path=path, rel_path=rel_path, content=text, truncated=truncated))
            remaining = max(0, remaining - len(data))
        if truncated:
            break

    if remaining <= 0 and len(docs) < len(paths):
        truncated_any = True
    return GaProjectInstructions(docs=tuple(docs), max_bytes=budget, truncated=truncated_any)


def build_ga_project_instructions(workspace_root=None, current_dir=None, max_bytes=None) -> str:
    loaded = load_ga_project_instructions(workspace_root, current_dir, max_bytes)
    if not loaded.docs:
        return ""

    lines = [
        "",
        "[GA_PROJECT_INSTRUCTIONS]",
        "The following project instructions are loaded from GA_AGENTS.md / GA_AGENTS.override.md.",
        "They are ordered from workspace root to current directory.",
        "When instructions conflict, follow the later and more specific source.",
    ]
    if loaded.truncated:
        lines.append(f"Some project instructions were truncated by the {loaded.max_bytes} byte budget.")
    for doc in loaded.docs:
        lines.extend([
            "",
            "--- ga-project-doc ---",
            f"Source: {doc.rel_path}",
        ])
        if doc.truncated:
            lines.append("Status: truncated")
        lines.append(doc.content.rstrip())
    lines.extend(["[/GA_PROJECT_INSTRUCTIONS]", ""])
    return "\n".join(lines)


__all__ = [
    "DEFAULT_GA_AGENTS_FILENAME",
    "LOCAL_GA_AGENTS_FILENAME",
    "DEFAULT_PROJECT_DOC_MAX_BYTES",
    "LoadedGaAgentsDoc",
    "GaProjectInstructions",
    "discover_ga_agents_paths",
    "load_ga_project_instructions",
    "build_ga_project_instructions",
]
