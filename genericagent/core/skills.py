import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Optional
from genericagent import paths


NL = chr(10)
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
    disable_model_invocation: bool = False
    user_invocable: bool = True
    scope: str = ""
    always_on_chars: int = 0
    on_invoke_chars: int = 0

    @property
    def qualified_name(self) -> str:
        return f"{self.scope}:{self.name}" if self.scope else self.name


def _home_dir() -> Path:
    return Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or str(Path.home()))


def get_default_skill_roots() -> list[Path]:
    roots = [
        paths.SKILLS_DIR,
        _home_dir() / ".claude" / "skills",
        _home_dir() / ".codex" / "skills",
    ]
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


def _parse_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    text = str(value or "").strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off"}:
        return False
    return default


def _skill_overrides() -> dict[str, str]:
    """Read optional per-skill listing overrides from GA_SKILL_OVERRIDES."""
    raw = os.environ.get("GA_SKILL_OVERRIDES", "").strip()
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    if not isinstance(parsed, dict):
        return {}
    valid = {"on", "name-only", "user-invocable-only", "off"}
    return {str(key): str(value) for key, value in parsed.items() if str(value) in valid}


def effective_skill_state(skill: "SkillSpec", overrides: dict[str, str] | None = None) -> str:
    """Return on / name-only / user-invocable-only / off for one skill."""
    mapping = _skill_overrides() if overrides is None else overrides
    state = mapping.get(skill.qualified_name) or mapping.get(skill.name)
    if state in {"on", "name-only", "user-invocable-only", "off"}:
        return state
    if skill.disable_model_invocation:
        return "user-invocable-only"
    return "on"


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


def _build_discovery_signature(roots: list[Path], cwd: Path) -> tuple:
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
    return (str(cwd), tuple(root_signatures))


def _directory_scope(skill_file: Path, cwd: Path) -> str:
    """Return a CC-style path prefix for a nested project ``.claude/skills`` entry.

    Claude Code names a skill found at ``<cwd>/apps/web/.claude/skills/deploy``
    as ``apps/web:deploy`` so a monorepo can keep a scoped variant beside an
    unscoped one.  Only emit a scope when ``cwd`` is an ancestor of the project
    root; a skill inside the current project remains unscoped.
    """
    parent = skill_file.parent
    for candidate in (parent, *parent.parents):
        if candidate.name.lower() != "skills" or candidate.parent.name.lower() != ".claude":
            continue
        project_root = candidate.parent.parent
        try:
            relative = project_root.relative_to(cwd)
        except ValueError:
            return ""
        if not relative.parts:
            return ""
        return relative.as_posix()
    return ""


def discover_skills(
    search_roots: Optional[Iterable[os.PathLike | str]] = None,
    *,
    cwd: Optional[os.PathLike | str] = None,
) -> list[SkillSpec]:
    roots = [_normalize_root(Path(raw_root)) for raw_root in (search_roots or get_default_skill_roots())]
    cwd_path = _normalize_root(Path(cwd)) if cwd is not None else _normalize_root(Path.cwd())
    signature = _build_discovery_signature(roots, cwd_path)
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
            scope = str(meta.get("scope") or "").strip().strip(":")
            if not scope:
                scope = _directory_scope(skill_file, cwd_path)
            allowed_tools = _parse_list(meta.get("allowed_tools") or meta.get("allowed"))
            disable_model_invocation = _parse_bool(meta.get("disable_model_invocation"))
            user_invocable = _parse_bool(meta.get("user_invocable"), True)
            qualified_name = f"{scope}:{name}" if scope else name
            dedupe_key = qualified_name.casefold()
            if dedupe_key in seen_names:
                continue
            seen_names.add(dedupe_key)
            always_on_chars = len(" ".join(part for part in (name, description, when_to_use) if part))
            on_invoke_chars = len(body.strip())
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
                    disable_model_invocation=disable_model_invocation,
                    user_invocable=user_invocable,
                    scope=scope,
                    always_on_chars=always_on_chars,
                    on_invoke_chars=on_invoke_chars,
                )
            )
    _DISCOVERY_CACHE[signature] = list(skills)
    return skills


# Claude Code caps one listing line at 1536 chars; GA compacts harder because the
# index also carries the user's Claude/Codex skills. 400 keeps the "what" plus the
# trigger sentence (see _compact_description) without eating the whole budget.
_SKILL_DESC_CAP = 400

# Claude Code derives its budget (window x 4 x 0.01 ~= 8000 at 200k) and allows an
# override. GA lists more skills (the local/Claude/Codex roots share one index), so
# the default is 2x CC's -- 16K keeps every description whole on this machine --
# and the env knob is the same escape hatch in both directions.
SKILL_LISTING_CHAR_BUDGET_ENV = "GA_SKILL_LISTING_CHAR_BUDGET"
DEFAULT_SKILL_LISTING_CHAR_BUDGET = 16384


def _skill_listing_budget() -> int:
    raw = str(os.environ.get(SKILL_LISTING_CHAR_BUDGET_ENV) or "").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return DEFAULT_SKILL_LISTING_CHAR_BUDGET
    return value if value > 0 else DEFAULT_SKILL_LISTING_CHAR_BUDGET


def _split_description_sentences(text: str) -> list[str]:
    # CJK sentences have no space after the full stop; ASCII ones need one, or
    # "e.g." / "1.5" would be cut apart.
    parts = re.split(r"(?<=[。！？；])|(?<=[.!?])\s+", text)
    return [part.strip() for part in parts if part.strip()]


# Phrases that carry the "when should I load this" signal. Skill descriptions put
# them at the END ("... Use when the user asks for X"), which a blind head-truncation
# cuts off -- and that trigger is exactly what an implicit-match scan needs.
_SKILL_TRIGGER_RE = re.compile(
    r"(use when|use this|use for|when the user|trigger|do not use|使用|触发|当用户|用来)",
    re.IGNORECASE,
)


def _compact_description(text: str, cap: int) -> str:
    """Trim a listing description without losing its trigger sentence.

    Claude Code caps one listing line at 1536 chars and lets the caller's budget
    degrade whole rows. GA lists far more skills (the user's Claude/Codex skills
    share the index), so it compacts harder -- but a blind head-cut drops the
    "Use when ..." tail that implicit matching depends on. Keep the head, then
    splice a (possibly shortened) trigger sentence back in.

    When the trigger is already the first sentence a plain head-fill keeps it,
    and splicing would print a confusing leading "..." -- so that case just
    fills from the front.
    """
    if len(text) <= cap:
        return text
    sentences = _split_description_sentences(text)
    trigger_index = next(
        (index for index, sentence in enumerate(sentences) if _SKILL_TRIGGER_RE.search(sentence)),
        None,
    )
    kept: list[str] = []
    used = 0

    def take_while(seq: list[str], limit: int) -> bool:
        nonlocal used
        for sentence in seq:
            if used + len(sentence) + 1 > limit:
                return False
            kept.append(sentence)
            used += len(sentence) + 1
        return True

    if trigger_index in (None, 0):
        complete = take_while(sentences, cap - 2)
        result = " ".join(kept).strip()
        if not complete:
            result = (result + " ...").strip()
        return (result or text[: max(0, cap - 1)].rstrip() + "...")[:cap].rstrip()

    take_while(sentences[:trigger_index], max(40, int(cap * 0.6)))
    remaining = cap - used - 2  # room for the "..." join marker
    if remaining > 24:
        trigger = sentences[trigger_index]
        if len(trigger) > remaining:
            trigger = trigger[: max(0, remaining - 1)].rstrip() + "..."
        kept.append("... " + trigger)
    else:
        kept = [text[: max(0, cap - 1)].rstrip() + "..."]
    return (" ".join(kept).strip())[:cap].rstrip()


def format_skill_listing(
    skills: Iterable[SkillSpec],
    char_budget: int = DEFAULT_SKILL_LISTING_CHAR_BUDGET,
    *,
    for_model: bool = True,
) -> str:
    lines: list[str] = []
    used = 0
    overrides = _skill_overrides()
    for skill in skills:
        state = effective_skill_state(skill, overrides)
        if for_model and state in {"off", "user-invocable-only"}:
            continue
        if not for_model and (state == "off" or not skill.user_invocable):
            continue
        name = skill.qualified_name
        desc = skill.description
        if skill.when_to_use:
            desc = f"{desc} - {skill.when_to_use}" if desc else skill.when_to_use
        desc = re.sub(r"\s+", " ", desc).strip()
        desc = _compact_description(desc, _SKILL_DESC_CAP)
        if state == "name-only":
            line = f"- {name}"
        else:
            line = f"- {name}: {desc}" if desc else f"- {name}"
            if skill.source:
                line += f" [{skill.source}]"
        extra = len(line) + (1 if lines else 0)
        if lines and used + extra > char_budget:
            # Degrade the whole row to `- name` -- never drop the skill. Claude
            # Code's algorithm keeps every skill in the listing and only spends
            # budget on descriptions; a `break` here made 15 of 53 skills
            # invisible to the model, which can then never match them.
            line = f"- {name}"
            extra = len(line) + (1 if lines else 0)
        if not lines and extra > char_budget:
            line = line[: max(0, char_budget - 3)].rstrip() + "..."
            extra = len(line)
        lines.append(line)
        used += extra
    return "\n".join(lines)


def find_skill(
    name: str,
    search_roots: Optional[Iterable[os.PathLike | str]] = None,
    *,
    cwd: Optional[os.PathLike | str] = None,
) -> SkillSpec:
    query = name.strip().lstrip("/")
    for skill in discover_skills(search_roots, cwd=cwd):
        if skill.qualified_name == query or skill.name == query:
            return skill
    available = ", ".join(s.qualified_name for s in discover_skills(search_roots, cwd=cwd)[:50])
    raise KeyError(f"Unknown skill: {query}" + (f". Available: {available}" if available else ""))


def load_skill_content(
    name: str,
    search_roots: Optional[Iterable[os.PathLike | str]] = None,
    args: str = "",
    *,
    invocation: str = "model",
    cwd: Optional[os.PathLike | str] = None,
) -> dict:
    skill = find_skill(name, search_roots, cwd=cwd)
    state = effective_skill_state(skill)
    if invocation == "model" and state in {"off", "user-invocable-only"}:
        raise PermissionError(f"Skill is not model-invocable: {skill.qualified_name} (state={state})")
    if invocation == "user" and (state == "off" or not skill.user_invocable):
        raise PermissionError(f"Skill is not user-invocable: {skill.qualified_name} (state={state})")
    base_dir = str(skill.root.resolve())
    content = (
        f"Base directory for this skill: {base_dir}{NL}{NL}"
        f"Attached files live under this base directory. Read only the reference you need with file_read.{NL}{NL}"
        f"{skill.body}"
    )
    normalized_base = base_dir.replace(chr(92), "/")
    content = content.replace("${CLAUDE_SKILL_DIR}", normalized_base)
    content = content.replace("${CODEX_SKILL_DIR}", normalized_base)
    content = content.replace("${GA_SKILL_DIR}", normalized_base)
    content = content.replace("$ARGUMENTS", args or "")
    return {
        "status": "success",
        "name": skill.qualified_name,
        "description": skill.description,
        "source": skill.source,
        "path": str(skill.path),
        "base_dir": base_dir,
        "allowed_tools": list(skill.allowed_tools),
        "state": state,
        "always_on_chars": skill.always_on_chars,
        "on_invoke_chars": skill.on_invoke_chars,
        "content": content,
    }


def skill_status_rows(
    search_roots: Optional[Iterable[os.PathLike | str]] = None,
    *,
    cwd: Optional[os.PathLike | str] = None,
) -> list[dict]:
    """Return the data needed by the /skills-style status view."""
    overrides = _skill_overrides()
    rows: list[dict] = []
    for skill in discover_skills(search_roots, cwd=cwd):
        state = effective_skill_state(skill, overrides)
        rows.append(
            {
                "name": skill.qualified_name,
                "description": skill.description,
                "source": skill.source,
                "path": str(skill.path),
                "state": state,
                "always_on_chars": skill.always_on_chars,
                "on_invoke_chars": skill.on_invoke_chars,
                "model_visible": state not in {"off", "user-invocable-only"},
                "user_invocable": skill.user_invocable and state != "off",
            }
        )
    return rows


def build_skill_prompt(char_budget: int | None = None) -> str:
    skills = discover_skills()
    budget = char_budget if char_budget is not None else _skill_listing_budget()
    listing = format_skill_listing(skills, char_budget=budget, for_model=True)
    if not listing:
        return ""
    return (
        f"{NL}[Available Skills]{NL}"
        "Before starting a task, scan this list and match by *task type* (diagnosis, design, review, workflow "
        "authoring, diagrams, slides), not only when the user names a skill. If any skill is even partially "
        "relevant to the task's domain, repository, process or deliverable, call load_skill and read the full "
        "SKILL.md first; do not skip it because you think you already know how. The listing is only an index; "
        "the loaded skill is authoritative. "
        f"{NL}{listing}{NL}"
    )
