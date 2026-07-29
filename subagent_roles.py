from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from subagent_agent_path import AgentPath
from subagent_permissions import normalize_permission_metadata


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
    source_path: str | None = None


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
        raise FileNotFoundError(name)

    def list_roles(self):
        if not self.roles_dir.is_dir():
            return []
        roles = []
        seen = set()
        for path in sorted([*self.roles_dir.glob("*.json"), *self.roles_dir.glob("*.md")]):
            name = _normalize_role_name(path.stem)
            if name in seen:
                continue
            seen.add(name)
            roles.append(self.get(name))
        return roles


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
    system_prompt = data.get("system_prompt") or data.get("systemPrompt") or body_prompt
    return SubagentRole(
        name=name,
        description=_none_if_empty(data.get("description")),
        when_to_use=_none_if_empty(data.get("when_to_use") or data.get("whenToUse")),
        system_prompt=_none_if_empty(system_prompt),
        permission_profile=permission["permission_profile"],
        permission_options=permission["options"],
        model_profile=_none_if_empty(data.get("model_profile") or data.get("modelProfile")),
        fork_turns_default=_none_if_empty(data.get("fork_turns_default") or data.get("forkTurnsDefault")),
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
