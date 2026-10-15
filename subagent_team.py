"""Implicit team identity for the GA Agent Team (aligned to Claude Code 2.1.283).

CC's build has no ``TeamCreate``: a session starts with one implicit team whose
lead is named ``team-lead`` (``mi``), and every spawned agent joins it.  GA
mirrors that by deriving the team name from the session id and treating the root
agent as the lead.

Data-only on purpose (names + a derived view) so it can be imported by the
manager, the tool layer and tests without an import cycle.
"""

from __future__ import annotations

import os


LEAD_AGENT_NAME = "team-lead"
RESERVED_AGENT_NAMES = frozenset({"main", "team-lead", "user", "system"})
TEAM_NAME_ENV = "GA_TEAM_NAME"


def is_reserved_name(name) -> bool:
    return str(name or "").strip().lower() in RESERVED_AGENT_NAMES


def team_name_for(session_id=None, env=None) -> str:
    """Implicit team name: ``GA_TEAM_NAME`` wins, else ``session-<id[:8]>`` (CC default).

    GA session ids are ``session_<uuid4hex>`` (see ``session_transcript.new_session_id``),
    so a naive ``sid[:8]`` would slice the constant ``session_`` prefix and collapse every
    session onto the same team name (and thus the same shared task board). Strip that
    prefix first so the 8-char suffix is actually session-unique.
    """
    source = os.environ if env is None else env
    explicit = str(source.get(TEAM_NAME_ENV) or "").strip()
    if explicit:
        return explicit
    sid = str(session_id or "").strip()
    if sid.startswith("session_"):
        sid = sid[len("session_"):]
    return "session-" + sid[:8] if sid else "default"


class SubagentTeam:
    """A thin view over the registry that answers 'who is on my team'."""

    def __init__(self, registry, self_agent_path, *, team_name=None, lead_name=LEAD_AGENT_NAME):
        self.registry = registry
        self.self_agent_path = str(self_agent_path).rstrip("/") or "/root"
        self.team_name = team_name or "default"
        self.lead_name = lead_name

    @property
    def lead_agent_id(self):
        return self.self_agent_path

    @staticmethod
    def validate_name(name):
        name = str(name or "").strip()
        if is_reserved_name(name):
            raise ValueError("agent name %r is reserved" % name)
        return name

    def members(self, include_closed=False):
        return self.registry.list_agents(include_closed=include_closed)

    def view(self, include_closed=False):
        members = []
        for entry in self.members(include_closed=include_closed):
            members.append(
                {
                    "agentId": str(entry.agent_path),
                    "name": entry.task_name,
                    "status": entry.status,
                    "parentPath": str(entry.parent_path) if entry.parent_path else None,
                    "runId": entry.run_id,
                }
            )
        return {
            "teamName": self.team_name,
            "leadAgentId": self.lead_agent_id,
            "leadName": self.lead_name,
            "memberCount": len(members),
            "members": members,
        }
