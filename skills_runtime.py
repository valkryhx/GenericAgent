import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional


FRONTMATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?", re.DOTALL)
_DISCOVERY_CACHE: dict[tuple, list["SkillSpec"]] = {}


@dataclass(frozen=True)
class SkillSpec:
    name: str
    description: str
    source: str
    root: Path
    path: Path
    body: str
    when_to_use: str = ""
    allowed_tools: tuple[str, ...] = ()
    version: str = ""


def _home_dir() -> Path:
    return Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or str(Path.home()))


def get_default_skill_roots() -> list[Path]:
    roots = [_home_dir() / ".claude" / "skills", _home_dir() / ".codex" / "skills"]
    extra = os.environ.get("GA_SKILL_PATHS", "")
    if extra:
        roots.extend(Path(p.strip()) for p in extra.split(os.pathsep) if p.strip())
    return roots


def _source_for_root(root: Path) -> str:
    parts = {p.lower() for p in root.parts}
    if ".claude" in parts:
        return "claude"
    if ".codex" in parts:
        return "codex"
    return "local"


def _parse_scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _parse_list(value: object) -> tuple[str, ...]:
    if isinstance(value, list):
        return tuple(str(x).strip() for x in value if str(x).strip())
    if isinstance(value, str):
        parts = re.split(r"[,;\n]", value)
        return tuple(p.strip() for p in parts if p.strip())
    return ()


def parse_skill_markdown(text: str, fallback_name: str) -> tuple[dict, str]:
    match = FRONTMATTER_RE.match(text)
    if not match:
        return {"name": fallback_name, "description": _extract_heading_description(text)}, text

    frontmatter = match.group(1)
    body = text[match.end():]
    data: dict[str, object] = {}
    current_key: Optional[str] = None
    for raw_line in frontmatter.splitlines():
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        item = re.match(r"\s*-\s*(.+?)\s*$", line)
        if item and current_key:
            existing = data.setdefault(current_key, [])
            if isinstance(existing, list):
                existing.append(_parse_scalar(item.group(1)))
            continue
        keyval = re.match(r"\s*([A-Za-z0-9_-]+)\s*:\s*(.*?)\s*$", line)
        if keyval:
            current_key = keyval.group(1).replace("-", "_")
            value = keyval.group(2)
            data[current_key] = _parse_scalar(value) if value else []

    data.setdefault("name", fallback_name)
    data.setdefault("description", _extract_heading_description(body))
    return data, body


def _extract_heading_description(markdown: str) -> str:
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            return stripped.lstrip("#").strip()
        if stripped:
            return stripped[:180]
    return ""


def _candidate_skill_files(root: Path) -> Iterable[Path]:
    if not root.exists():
        return []
    if root.is_file() and root.name.lower() == "skill.md":
        return [root]
    if (root / "SKILL.md").is_file():
        return [root / "SKILL.md"]
    try:
        files = [p for p in root.rglob("SKILL.md") if p.is_file()]
    except OSError:
        return []
    return sorted(files, key=lambda p: tuple(part.lower() for part in p.relative_to(root).parts))


def clear_skill_cache() -> None:
    _DISCOVERY_CACHE.clear()


def _normalize_root(root: Path) -> Path:
    return root.expanduser().resolve(strict=False)


def _build_discovery_signature(roots: list[Path]) -> tuple:
    root_signatures = []
    for root in roots:
        file_entries = []
        for skill_file in _candidate_skill_files(root):
            try:
                stat = skill_file.stat()
                rel = str(skill_file.relative_to(root))
            except OSError:
                continue
            except ValueError:
                rel = str(skill_file)
            file_entries.append((rel, stat.st_mtime_ns, stat.st_size))
        root_signatures.append((str(root), tuple(file_entries)))
    return tuple(root_signatures)


def discover_skills(search_roots: Optional[Iterable[os.PathLike | str]] = None) -> list[SkillSpec]:
    roots = [_normalize_root(Path(raw_root)) for raw_root in (search_roots or get_default_skill_roots())]
    signature = _build_discovery_signature(roots)
    cached = _DISCOVERY_CACHE.get(signature)
    if cached is not None:
        return list(cached)

    skills: list[SkillSpec] = []
    seen_names: set[str] = set()
    for root in roots:
        source = _source_for_root(root)
        for skill_file in _candidate_skill_files(root):
            try:
                text = skill_file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            fallback_name = skill_file.parent.name
            meta, body = parse_skill_markdown(text, fallback_name)
            name = str(meta.get("name") or fallback_name).strip() or fallback_name
            description = str(meta.get("description") or "").strip()
            when_to_use = str(meta.get("when_to_use") or meta.get("when") or "").strip()
            version = str(meta.get("version") or "").strip()
            allowed_tools = _parse_list(meta.get("allowed_tools") or meta.get("allowed"))
            dedupe_key = name.casefold()
            if dedupe_key in seen_names:
                continue
            seen_names.add(dedupe_key)
            skills.append(
                SkillSpec(
                    name=name,
                    description=description,
                    source=source,
                    root=skill_file.parent,
                    path=skill_file,
                    body=body,
                    when_to_use=when_to_use,
                    allowed_tools=allowed_tools,
                    version=version,
                )
            )
    _DISCOVERY_CACHE[signature] = list(skills)
    return skills


def format_skill_listing(skills: Iterable[SkillSpec], char_budget: int = 8000) -> str:
    lines: list[str] = []
    used = 0
    for skill in skills:
        desc = skill.description
        if skill.when_to_use:
            desc = f"{desc} - {skill.when_to_use}" if desc else skill.when_to_use
        desc = re.sub(r"\s+", " ", desc).strip()
        if len(desc) > 250:
            desc = desc[:249].rstrip() + "..."
        line = f"- {skill.name}: {desc}" if desc else f"- {skill.name}"
        if skill.source:
            line += f" [{skill.source}]"
        extra = len(line) + (1 if lines else 0)
        if lines and used + extra > char_budget:
            compact = f"- {skill.name}"
            compact_extra = len(compact) + 1
            if used + compact_extra <= char_budget:
                lines.append(compact)
                used += compact_extra
                continue
            break
        if not lines and extra > char_budget:
            line = line[: max(0, char_budget - 3)].rstrip() + "..."
            extra = len(line)
        lines.append(line)
        used += extra
    return "\n".join(lines)


def find_skill(name: str, search_roots: Optional[Iterable[os.PathLike | str]] = None) -> SkillSpec:
    query = name.strip().lstrip("/")
    for skill in discover_skills(search_roots):
        if skill.name == query:
            return skill
    available = ", ".join(s.name for s in discover_skills(search_roots)[:50])
    raise KeyError(f"Unknown skill: {query}" + (f". Available: {available}" if available else ""))


def load_skill_content(
    name: str,
    search_roots: Optional[Iterable[os.PathLike | str]] = None,
    args: str = "",
) -> dict:
    skill = find_skill(name, search_roots)
    base_dir = str(skill.root.resolve())
    content = f"Base directory for this skill: {base_dir}\n\n{skill.body}"
    normalized_base = base_dir.replace("\\", "/")
    content = content.replace("${CLAUDE_SKILL_DIR}", normalized_base)
    content = content.replace("${CODEX_SKILL_DIR}", normalized_base)
    content = content.replace("${GA_SKILL_DIR}", normalized_base)
    content = content.replace("$ARGUMENTS", args or "")
    return {
        "status": "success",
        "name": skill.name,
        "description": skill.description,
        "source": skill.source,
        "path": str(skill.path),
        "base_dir": base_dir,
        "allowed_tools": list(skill.allowed_tools),
        "content": content,
    }


def build_skill_prompt(char_budget: int = 8000) -> str:
    skills = discover_skills()
    listing = format_skill_listing(skills, char_budget=char_budget)
    if not listing:
        return ""
    return (
        "\n[Available Skills]\n"
        "When a user task matches one of these skills, call load_skill with the skill name before answering or acting. "
        "The listing is only an index; load the full SKILL.md before following it.\n"
        f"{listing}\n"
    )
