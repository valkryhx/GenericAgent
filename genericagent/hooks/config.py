"""Hook configuration discovery, validation, and source ordering."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from genericagent import paths
from genericagent.hooks.models import EVENT_NAMES, HANDLER_TYPES, HookEntry, HookHandler


@dataclass
class HookConfigIssue:
    source: str
    path: str
    message: str


@dataclass
class HookRegistry:
    entries: list[HookEntry] = field(default_factory=list)
    issues: list[HookConfigIssue] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    # CC m0062:8024-8025. ``None`` means the key was not configured:
    # all HTTP URLs / handler-declared env vars are allowed. An empty list is
    # an explicit deny-all policy and must remain distinguishable from absent.
    allowed_http_hook_urls: list[str] | None = None
    http_hook_allowed_env_vars: list[str] | None = None

    def for_event(self, event: str) -> list[HookEntry]:
        return [entry for entry in self.entries if entry.event == event]

    def add_issue(self, source: str, path: str, message: str) -> None:
        self.issues.append(HookConfigIssue(source=source, path=path, message=message))


def _default_timeout(kind: str) -> int:
    return {"command": 10, "prompt": 30, "agent": 60, "http": 30, "mcp_tool": 30}.get(kind, 10)


def _timeout(value: Any, kind: str) -> int:
    try:
        timeout = int(value if value is not None else _default_timeout(kind))
    except (TypeError, ValueError):
        timeout = _default_timeout(kind)
    return max(1, min(timeout, 600))


def _parse_handler(raw: Any, registry: HookRegistry, source: str, path: str) -> HookHandler | None:
    if not isinstance(raw, dict):
        registry.add_issue(source, path, "hook handler must be an object")
        return None
    kind = str(raw.get("type") or "").strip()
    if kind not in HANDLER_TYPES:
        registry.add_issue(source, path, f"unsupported hook handler type: {kind or '<missing>'}")
        return None
    options = dict(raw)
    options["type"] = kind
    options["timeout"] = _timeout(raw.get("timeout"), kind)
    if kind == "command":
        if (raw.get("async") is True or raw.get("asyncRewake") is True) and raw.get("timeout") is None:
            # CC config-based command async falls back to 600s.
            options["timeout"] = 600
    if kind != "command" and (raw.get("async") is True or raw.get("asyncRewake") is True):
        registry.add_issue(source, path, f"{kind} handler does not support async/asyncRewake")
        return None
    if kind == "command":
        command = raw.get("command")
        args = raw.get("args")
        if not isinstance(command, str) or not command.strip():
            registry.add_issue(source, path, "command handler requires non-empty string field: command")
            return None
        if args is not None and (not isinstance(args, list) or any(not isinstance(item, str) for item in args)):
            registry.add_issue(source, path, "command handler field args must be an array of strings")
            return None
    elif kind in ("prompt", "agent"):
        if not isinstance(raw.get("prompt"), str) or not raw.get("prompt", "").strip():
            registry.add_issue(source, path, f"{kind} handler requires non-empty string field: prompt")
            return None
    elif kind == "http":
        if not isinstance(raw.get("url"), str) or not raw.get("url", "").strip():
            registry.add_issue(source, path, "http handler requires non-empty string field: url")
            return None
    elif kind == "mcp_tool":
        if not isinstance(raw.get("tool"), str) or not raw.get("tool", "").strip():
            registry.add_issue(source, path, "mcp_tool handler requires a non-empty tool field")
            return None
        server = raw.get("server")
        if server is not None and not isinstance(server, str):
            registry.add_issue(source, path, "mcp_tool handler field server must be a string")
            return None
    return HookHandler(kind=kind, options=options)


def _parse_file(path: Path, source: str, source_rank: int, registry: HookRegistry) -> None:
    registry.files.append(str(path))
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        registry.add_issue(source, str(path), f"cannot read hook configuration: {exc}")
        return
    if isinstance(data, dict):
        for key, target in (
            ("allowedHttpHookUrls", "allowed_http_hook_urls"),
            ("httpHookAllowedEnvVars", "http_hook_allowed_env_vars"),
        ):
            if key not in data:
                continue
            value = data.get(key)
            if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
                registry.add_issue(source, str(path), f"{key} must be an array of strings")
                continue
            current = getattr(registry, target)
            # Settings sources union these restriction lists (CC m0062:7730).
            merged = list(dict.fromkeys((current or []) + value)) if current is not None else list(dict.fromkeys(value))
            setattr(registry, target, merged)
    hooks = data.get("hooks") if isinstance(data, dict) else None
    if hooks is None and isinstance(data, dict):
        # A bare event map is accepted only when it doesn't look like a full
        # settings object. This keeps hook files concise without treating
        # policy keys as event names.
        hooks = {k: v for k, v in data.items() if k in EVENT_NAMES}
    if not isinstance(hooks, dict):
        registry.add_issue(source, str(path), "hook configuration must contain an object field: hooks")
        return
    for event, entries in hooks.items():
        event_name = str(event)
        if event_name not in EVENT_NAMES:
            registry.add_issue(source, str(path), f"unknown hook event: {event_name}")
            continue
        if not isinstance(entries, list):
            registry.add_issue(source, str(path), f"hook event {event_name} must be an array")
            continue
        for entry_index, raw_entry in enumerate(entries):
            if not isinstance(raw_entry, dict):
                registry.add_issue(source, str(path), f"{event_name}[{entry_index}] must be an object")
                continue
            raw_handlers = raw_entry.get("hooks")
            if not isinstance(raw_handlers, list):
                registry.add_issue(source, str(path), f"{event_name}[{entry_index}].hooks must be an array")
                continue
            handlers = []
            for raw_handler in raw_handlers:
                parsed = _parse_handler(raw_handler, registry, source, str(path))
                if parsed is not None:
                    handlers.append(parsed)
            if not handlers:
                continue
            matcher = raw_entry.get("matcher")
            if matcher is not None and not isinstance(matcher, str):
                registry.add_issue(source, str(path), f"{event_name}[{entry_index}].matcher must be a string")
                continue
            registry.entries.append(HookEntry(
                event=event_name,
                matcher=matcher,
                handlers=tuple(handlers),
                source=source,
                source_path=str(path),
                source_rank=source_rank,
                entry_index=entry_index,
            ))


def _home_dir() -> Path:
    return Path(os.environ.get("USERPROFILE") or os.environ.get("HOME") or Path.home())


def discover_hook_files(
    project_root: str | Path | None = None,
    home: str | Path | None = None,
    *,
    config_dir: str | Path | None = None,
) -> list[tuple[str, int, Path]]:
    """Return ``(source, rank, path)`` pairs in ascending execution order.

    ``project_root`` keeps the historical production default of
    ``<project>/.genericagent``.  Callers that already own the hook directory
    (tests, embedded hosts, or a future project-local config chooser) should
    pass ``config_dir`` explicitly instead of guessing whether the argument is
    a project root or the config directory itself.
    """
    explicit = str(os.environ.get("GA_HOOKS_CONFIG") or "").strip()
    if explicit:
        return [("explicit", 100, Path(explicit).expanduser())]
    project = Path(config_dir) if config_dir is not None else Path(project_root or paths.PROJECT_ROOT) / ".genericagent"
    if home is not None:
        user = Path(home)
        if user.name != ".genericagent":
            user = user / ".genericagent"
    else:
        user = _home_dir() / ".genericagent"
    candidates = [
        ("project", 30, project / "settings.json"),
        ("project", 35, project / "hooks.json"),
        ("local", 40, project / "settings.local.json"),
        ("user", 50, user / "settings.json"),
        ("user", 55, user / "hooks.json"),
    ]
    return [(source, rank, path) for source, rank, path in candidates if path.is_file()]


def load_hook_registry(
    project_root: str | Path | None = None,
    home: str | Path | None = None,
    *,
    config_dir: str | Path | None = None,
) -> HookRegistry:
    registry = HookRegistry()
    if str(os.environ.get("GA_HOOKS_DISABLED") or "").strip().lower() in {"1", "true", "yes", "on"}:
        return registry
    for source, rank, path in discover_hook_files(project_root=project_root, home=home, config_dir=config_dir):
        _parse_file(path, source, rank, registry)
    registry.entries.sort(key=lambda item: (item.source_rank, item.source_path, item.entry_index))
    return registry
