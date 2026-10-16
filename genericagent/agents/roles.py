from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from subagent_agent_path import AgentPath
from subagent_permissions import normalize_permission_metadata
from workflow_permissions import READ_ONLY


_ROLE_OPTION_KEYS = (
    "allowed_tools",
    "denied_tools",
    "allowed_mcp_servers",
    "denied_mcp_servers",
    "allowed_mcp_tools",
    "denied_mcp_tools",
)


@dataclass(frozen=True)
class SubagentRole:
    name: str
    description: str | None = None
    when_to_use: str | None = None
    system_prompt: str | None = None
    permission_profile: str | None = None
    permission_options: dict | None = None
    model_profile: str | None = None
    fork_turns_default: str | None = None
    tools: tuple[str, ...] = ()
    allow_delegation: bool = False
    source_path: str | None = None


# Built-in roles, shipped so a fresh install has the same useful defaults as the
# reference harnesses: pi ships scout/planner/reviewer/worker agents and Step-Code
# ships general/explore/review/planner, each with a capability note in the tool
# description ("prefer a read-only agent for review, audit or exploration").
#
# GA used to ship none, so the model had to hand-build the boundary out of
# ``permission_profile`` + ``allowed_tools`` on every spawn -- or, more often,
# spawn a full-access child for work that should have been read-only. These
# entries are all read-only; anything that must write stays a generic spawn or a
# project role under ``.ga/subagents``.
BUILTIN_ROLES: tuple[SubagentRole, ...] = (
    SubagentRole(
        name="explore",
        description="reconnaissance: precise paths and line references",
        when_to_use=(
            "Mapping an unfamiliar area before anything changes. Cheaper and safer than reading "
            "the same files into the main context."
        ),
        system_prompt=(
            "Explore the assigned area and report precise findings: exact paths with line references, "
            "the interfaces that matter, and the constraints a later agent must respect. Do not modify "
            "anything, and do not propose a redesign."
        ),
        permission_profile=READ_ONLY,
        source_path="builtin:explore",
    ),
    SubagentRole(
        name="plan",
        description="implementation plan: ordered steps naming file, change and check",
        when_to_use="Turning gathered context into an executable plan before any edit begins.",
        system_prompt=(
            "Produce an implementation plan: ordered, actionable steps that each name the file, the "
            "change and the check, plus risks and open questions. Do not modify files."
        ),
        permission_profile=READ_ONLY,
        source_path="builtin:plan",
    ),
    SubagentRole(
        name="review",
        description="review: findings first, ordered by severity with file:line",
        when_to_use="An independent check of work the parent or another agent produced.",
        system_prompt=(
            "Review independently and report findings first, ordered by severity with file:line "
            "references, then open questions and residual risks. Report only actionable findings; "
            "do not modify files."
        ),
        permission_profile=READ_ONLY,
        source_path="builtin:review",
    ),
)


def _builtin_role(name):
    for role in BUILTIN_ROLES:
        if role.name == name:
            return role
    return None


class SubagentRoleRegistry:
    def __init__(self, root_dir):
        self.root_dir = Path(root_dir)
        self.roles_dir = self.root_dir / ".ga" / "subagents"

    def get(self, name):
        name = _normalize_role_name(name)
        for suffix in (".json", ".md"):
            path = self.roles_dir / f"{name}{suffix}"
            if path.is_file():
                return _load_role_file(path, default_name=name)
        builtin = _builtin_role(name)
        if builtin is not None:
            return builtin
        raise FileNotFoundError(name)

    def list_roles(self):
        """Configured roles first, then built-ins that no project role overrode."""
        roles = []
        seen = set()
        if self.roles_dir.is_dir():
            for path in sorted([*self.roles_dir.glob("*.json"), *self.roles_dir.glob("*.md")]):
                name = _normalize_role_name(path.stem)
                if name in seen:
                    continue
                seen.add(name)
                roles.append(self.get(name))
        for role in BUILTIN_ROLES:
            if role.name not in seen:
                seen.add(role.name)
                roles.append(role)
        return roles


def role_capability_note(role) -> str:
    """One short capability note per role, derived from its own boundary.

    Step-Code derives its subagent guidance from the agent catalog so the tool
    description cannot drift from what the agents can actually do. The note is
    read from ``permission_profile`` rather than a hand-written label for the
    same reason.
    """

    profile = str(getattr(role, "permission_profile", "") or "").strip().lower()
    return "read-only" if profile == READ_ONLY else "can write"


def format_role_catalog(roles, *, language: str = "en") -> str:
    """Render ``name (capability note)`` for every role, in one sentence.

    The capability note always comes from the role's own ``permission_profile``;
    a description is appended as detail when the role has one. A hand-written
    label cannot drift from the boundary, which is the whole point of deriving
    this text instead of listing names.
    """

    entries = []
    for role in roles or ():
        name = str(getattr(role, "name", "") or "").strip()
        if not name:
            continue
        note = role_capability_note(role)
        detail = str(getattr(role, "description", "") or "").strip()
        if detail:
            note = f"{note} — {detail}"
        entries.append(f"{name} ({note})")
    if not entries:
        return ""
    if language == "zh":
        return "各自的能力：" + "、".join(entries) + "。"
    return "Capabilities: " + "; ".join(entries) + "."


def build_role_task_message(role, message):
    lines = ["[GA_SUBAGENT_ROLE]", f"name: {role.name}"]
    if role.description:
        lines.append(f"description: {role.description}")
    if role.when_to_use:
        lines.append(f"when_to_use: {role.when_to_use}")
    if role.system_prompt:
        lines.extend(["system_prompt:", str(role.system_prompt).strip()])
    lines.extend(["[/GA_SUBAGENT_ROLE]", "", "Task:", str(message or "").strip()])
    return "\n".join(lines).rstrip() + "\n"


def _load_role_file(path, *, default_name):
    if path.suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        body_prompt = None
    else:
        data, body_prompt = _read_markdown_role(path)
    if not isinstance(data, dict):
        data = {}
    name = _normalize_role_name(data.get("name") or default_name)
    permission_raw = {"permission_profile": data.get("permission_profile") or data.get("permissionProfile")}
    for key in _ROLE_OPTION_KEYS:
        if data.get(key) is not None:
            permission_raw[key] = data.get(key)
    permission = normalize_permission_metadata(permission_raw)
    raw_tools = data.get("tools")
    if raw_tools is None:
        raw_tools = data.get("capability_tools")
    if raw_tools is None:
        raw_tools = data.get("allowed_tools")
    if isinstance(raw_tools, str):
        raw_tools = [raw_tools]
    tools = tuple(str(item).strip() for item in (raw_tools or ()) if str(item).strip())
    allow_delegation = bool(data.get("allow_delegation", data.get("allowDelegation", False)))
    system_prompt = data.get("system_prompt") or data.get("systemPrompt") or body_prompt
    return SubagentRole(
        name=name,
        description=_none_if_empty(data.get("description")),
        when_to_use=_none_if_empty(data.get("when_to_use") or data.get("whenToUse")),
        system_prompt=_none_if_empty(system_prompt),
        permission_profile=permission["permission_profile"],
        permission_options=permission["options"],
        model_profile=_none_if_empty(data.get("model_profile") or data.get("modelProfile") or data.get("model")),
        fork_turns_default=_none_if_empty(data.get("fork_turns_default") or data.get("forkTurnsDefault")),
        tools=tools,
        allow_delegation=allow_delegation,
        source_path=str(path),
    )


def _read_markdown_role(path):
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        return {}, text.strip()
    end = text.find("\n---", 4)
    if end < 0:
        return {}, text.strip()
    header = text[4:end]
    body = text[end + len("\n---") :].strip()
    return _parse_frontmatter(header), body


def _parse_frontmatter(header):
    data = {}
    for line in header.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or ":" not in line:
            continue
        key, value = line.split(":", 1)
        data[key.strip()] = _parse_frontmatter_value(value.strip())
    return data


def _parse_frontmatter_value(value):
    if not value:
        return ""
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [_strip_quotes(part.strip()) for part in inner.split(",") if part.strip()]
    return _strip_quotes(value)


def _strip_quotes(value):
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _normalize_role_name(name):
    name = str(name or "").strip()
    AgentPath.root().join(name)
    return name


def _none_if_empty(value):
    if value is None:
        return None
    text = str(value).strip()
    return text or None
