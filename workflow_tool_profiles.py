"""Host-owned tool profiles for workflow children.

Mirrors two reference implementations:

* Step-Code's named ``WORKFLOW_TOOL_PROFILES`` (planner/developer/qa): the plan
  picks a *profile*, the host turns it into the child's tool set.
* Codex's ``ToolPolicy.allowed_tools``: "a startup ceiling on tool selection ...
  this policy only restricts tools".

Both references only ever *restrict*; neither requires the model to call a
specific tool by name.  A model-declared tool name is a guess that breaks the
moment a server is renamed, disabled, or fails to connect -- which is exactly
what happened when ``mcp__fetch__*`` disappeared and the research child fell
back to hand-rolled ``code_run`` scraping.

Profiles are expressed as **capability classes**, not tool-name allowlists, so a
tool added later automatically lands in the right class.  Same lesson as the
artifact-observation fix: classify by behaviour, never by a name list that goes
stale.
"""

from __future__ import annotations

from subagent_capabilities import ORCHESTRATION_TOOLS


CAPABILITY_CLASSES = (
    "web_search",
    "web_fetch",
    "file_read",
    "file_write",
    "execute",
    "orchestration",
)

EVIDENCE_CAPABILITIES = ("web_search", "web_fetch", "file_write", "file_read", "execute")

MUTATING_TOOL_NAMES = frozenset({"file_write", "file_patch"})
EXECUTE_TOOL_NAMES = frozenset({"code_run", "web_execute_js"})
READ_TOOL_NAMES = frozenset({"file_read"})

_SEARCH_TOKENS = ("search", "query", "find", "lookup", "research")
_FETCH_TOKENS = ("fetch", "extract", "crawl", "read", "get", "open", "download", "browse", "map")
_MUTATING_TOKENS = ("write", "create", "update", "delete", "remove", "save", "put", "insert", "upload", "edit", "patch", "append", "rename", "move", "mkdir")
_EXECUTE_TOKENS = ("execute", "exec", "run", "eval", "shell", "command", "bash", "powershell", "python")


# Named profiles -> denied capability classes.  An absent name means "*".
# Orchestration is never listed: it is denied for every workflow child.
WORKFLOW_TOOL_PROFILES: dict[str, frozenset[str]] = {
    "planner": frozenset({"file_write", "execute"}),
    "research": frozenset({"execute"}),
    "authoring": frozenset(),
    "verify": frozenset({"file_write"}),
}

# A plan that names no profile still gets a boundary derived from its declared
# role, exactly like `resolve_job_permission_profile` does for evidence roles.
ROLE_DEFAULT_TOOL_PROFILE: dict[str, str] = {
    "verification": "verify",
    "review": "verify",
    "research": "research",
    "understanding": "planner",
    "synthesis": "planner",
    "implementation": "authoring",
    "tests": "authoring",
    "repair": "authoring",
}

DEFAULT_TOOL_PROFILE = "authoring"
UNRESTRICTED_TOOL_PROFILE = "*"

ALWAYS_DENIED_CAPABILITIES = frozenset({"orchestration"})


def normalize_tool_name(value) -> str:
    name = str(value or "").strip()
    if name.startswith("functions."):
        name = name[len("functions.") :]
    return name


def _token_parts(name: str) -> list[str]:
    normalized = normalize_tool_name(name).lower().replace("-", "_").replace(".", "_")
    return [part for part in normalized.split("_") if part]


def tool_capabilities(tool_name) -> frozenset[str]:
    """Classify one tool name into capability classes.

    Unknown names classify as empty: an unrecognised tool is not implicitly
    granted anything, and it is not silently treated as dangerous either.
    """

    name = normalize_tool_name(tool_name)
    if not name:
        return frozenset()
    if name in ORCHESTRATION_TOOLS:
        return frozenset({"orchestration"})

    capabilities: set[str] = set()
    if name in MUTATING_TOOL_NAMES:
        capabilities.add("file_write")
    if name in EXECUTE_TOOL_NAMES:
        capabilities.add("execute")
    if name in READ_TOOL_NAMES:
        capabilities.add("file_read")

    if name in MUTATING_TOOL_NAMES | EXECUTE_TOOL_NAMES | READ_TOOL_NAMES:
        # Exact static tools keep exactly the class they were declared with; the
        # token heuristics below are only for MCP/extension tools whose names we
        # do not control.
        return frozenset(capabilities)

    if name.startswith("mcp__"):
        parts = name.split("__", 2)
        leaf = parts[2] if len(parts) == 3 else name
        tokens = _token_parts(leaf)
        if any(token in _EXECUTE_TOKENS for token in tokens):
            capabilities.add("execute")
        if any(token in _MUTATING_TOKENS for token in tokens):
            capabilities.add("file_write")
        if any(token in _SEARCH_TOKENS for token in tokens):
            capabilities.add("web_search")
        elif any(token in _FETCH_TOKENS for token in tokens):
            capabilities.add("web_fetch")
        return frozenset(capabilities)

    if "web_scan" in name or "web_search" in name:
        capabilities.add("web_search")
    elif any(token in _FETCH_TOKENS for token in _token_parts(name)):
        capabilities.add("web_fetch")
    return frozenset(capabilities)


def tool_has_capability(tool_name, capability: str) -> bool:
    return str(capability or "") in tool_capabilities(tool_name)


def resolve_tool_profile(profile) -> frozenset[str] | None:
    """Return the denied capability classes for a profile.

    ``None`` means "no restriction beyond the always-denied classes" (``"*"``).
    Unknown names raise so a typo is a plan error, not a silent widening.
    """

    if profile is None:
        return None
    if isinstance(profile, (list, tuple, set, frozenset)):
        denied: set[str] = set()
        for item in profile:
            resolved = resolve_tool_profile(item)
            if resolved is None:
                return None
            denied.update(resolved)
        return frozenset(denied)
    name = str(profile).strip()
    if not name:
        return None
    if name == UNRESTRICTED_TOOL_PROFILE:
        return None
    if name not in WORKFLOW_TOOL_PROFILES:
        raise ValueError(
            "unknown workflow tool profile "
            f"{name!r}; known: {', '.join(sorted(WORKFLOW_TOOL_PROFILES))}, *"
        )
    return WORKFLOW_TOOL_PROFILES[name]


def is_known_tool_profile(profile) -> bool:
    try:
        resolve_tool_profile(profile)
    except ValueError:
        return False
    return True


def profile_for_role(role) -> str:
    name = str(role or "").strip().lower()
    return ROLE_DEFAULT_TOOL_PROFILE.get(name, DEFAULT_TOOL_PROFILE)


def effective_tool_profile(options: dict | None) -> tuple[str, frozenset[str] | None]:
    """Resolve ``(profile_name, denied_capabilities)`` for one agent packet.

    An explicit ``toolProfile`` wins; otherwise the declared role picks the
    boundary.  ``"*"`` is honoured as an explicit opt-out, which keeps the door
    open for a plan that genuinely needs every tool.
    """

    options = options if isinstance(options, dict) else {}
    declared = options.get("toolProfile")
    if declared is None or (isinstance(declared, str) and not declared.strip()):
        role = options.get("role")
        declared = profile_for_role(role) if role else DEFAULT_TOOL_PROFILE
    if isinstance(declared, (list, tuple, set, frozenset)):
        if not declared:
            return UNRESTRICTED_TOOL_PROFILE, None
        return ",".join(str(item) for item in declared), resolve_tool_profile(declared)
    name = str(declared).strip()
    return name, resolve_tool_profile(name)


def denied_capabilities(profile) -> frozenset[str]:
    denied = resolve_tool_profile(profile)
    if denied is None:
        return ALWAYS_DENIED_CAPABILITIES
    return frozenset(denied) | ALWAYS_DENIED_CAPABILITIES


def tool_allowed_by_profile(tool_name, profile) -> bool:
    return not (tool_capabilities(tool_name) & denied_capabilities(profile))


def filter_schema_for_profile(schema, profile) -> list:
    """Return the subset of ``schema`` a child under ``profile`` may use."""

    return filter_schema_for_denied(schema, denied_capabilities(profile))


def filter_schema_for_denied(schema, denied) -> list:
    """Same as :func:`filter_schema_for_profile` for an already-resolved deny set."""

    denied = frozenset(denied) if denied is not None else ALWAYS_DENIED_CAPABILITIES
    denied = denied | ALWAYS_DENIED_CAPABILITIES
    filtered: list = []
    for item in schema or []:
        if not isinstance(item, dict):
            continue
        function = item.get("function") or {}
        name = function.get("name") if isinstance(function, dict) else None
        if not (tool_capabilities(name) & denied):
            filtered.append(item)
    return filtered


def tool_names_for_capability(capability, tool_names) -> list[str]:
    """Concrete tools in ``tool_names`` that provide ``capability``."""

    wanted = str(capability or "").strip()
    if not wanted:
        return []
    return sorted({normalize_tool_name(name) for name in tool_names or () if tool_has_capability(name, wanted)})


def capability_coverage(tool_names, capabilities=None) -> dict[str, list[str]]:
    wanted = capabilities if capabilities is not None else EVIDENCE_CAPABILITIES
    return {str(capability): tool_names_for_capability(capability, tool_names) for capability in wanted}


def unavailable_capabilities(tool_names, capabilities=None) -> list[str]:
    return sorted(
        name for name, tools in capability_coverage(tool_names, capabilities).items() if not tools
    )
