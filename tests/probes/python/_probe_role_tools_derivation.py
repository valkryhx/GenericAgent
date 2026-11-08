"""Trace what spawn_agent actually passes as role_tools for an ordinary spawn.

The probe above showed build_subagent_capability_profile turns ``role_tools=[]``
(empty, but not None) into an empty allow-list, which then yields only
TEAMMATE_TOOLS when team_mode is on -- exactly the "task_* / send_message /
team_status" tool set the four blocked research subagents reported.

This walks the handler's own argument handling to see whether an ordinary spawn
produces None or [] there, and what a builtin role (whose ``tools`` defaults to
an empty tuple) produces.
"""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[3]
sys.path.insert(0, str(REPO))


def show(label, args, role):
    """Mirror handler.do_spawn_agent's role_tools derivation."""
    requested_role_tools = args.get("tools") or args.get("capability_tools")
    if requested_role_tools is None and role is not None:
        requested_role_tools = list(role.tools)
    if isinstance(requested_role_tools, str):
        requested_role_tools = [requested_role_tools]
    capability_options = {
        "role_tools": (
            None
            if requested_role_tools is None
            else [str(item) for item in requested_role_tools if str(item).strip()]
        ),
        "allow_delegation": bool(args.get("allow_delegation")),
        "team_mode": bool(args.get("team_mode")),
    }
    print(f"{label:<52} role_tools={capability_options['role_tools']!r}")


def main():
    from genericagent.agents.roles import BUILTIN_ROLES

    explore = next(r for r in BUILTIN_ROLES if r.name == "explore")
    print("builtin 'explore'.tools =", explore.tools, "  (type", type(explore.tools).__name__ + ")")
    print()
    show("plain spawn, no role, no tools", {}, None)
    show("plain spawn, team_mode=True", {"team_mode": True}, None)
    show("agent_type='explore' (builtin, tools=())", {}, explore)
    show("agent_type='explore' + team_mode", {"team_mode": True}, explore)
    show("explicit tools=['file_read']", {"tools": ["file_read"]}, None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
