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
RESERVED_AGENT_NAMES = frozenset({"main", "team-lead", "user", "system", "root"})
TEAM_NAME_ENV = "GA_TEAM_NAME"
# The ``agent_type`` a workflow stamps on the children it spawns
# (``genericagent/workflow/child_agent.py``). Rows carrying it are the workflow run's
# children, not team members, even though they live in the same shared registry.
WORKFLOW_CHILD_AGENT_TYPE = "workflow_child"


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

    def __init__(self, registry, self_agent_path, *, team_name=None, lead_name=LEAD_AGENT_NAME, session_id=None):
        self.registry = registry
        self.self_agent_path = str(self_agent_path).rstrip("/") or "/root"
        self.team_name = team_name or "default"
        self.lead_name = lead_name
        # The session that owns this team. Rows spawned by another session are that
        # session's team; ``None`` disables the check (callers that only know the team
        # name still get the workflow-child filter).
        self.session_id = str(session_id) if session_id else None

    @property
    def lead_agent_id(self):
        return self.self_agent_path

    @staticmethod
    def validate_name(name):
        name = str(name or "").strip()
        if is_reserved_name(name):
            raise ValueError("agent name %r is reserved" % name)
        return name

    def _is_team_member(self, entry):
        """Whether a registry row belongs to *this* team.

        The registry is shared by everything a GA process spawns, so listing it
        wholesale turns unrelated children into "teammates". Observed in a real run: a
        workflow was started with 3 subagents and, while it was still running, the
        agent-team overlay listed them as team members (their rows carried the workflow
        runId, and the task board read ``[working]`` while the roster said every member
        was idle).

        Two ownership markers decide it:

          * ``agent_type == "workflow_child"`` -- the row belongs to a workflow run.
            This is the durable marker: it survives restarts, and it covers nested
            children, because a workflow child that spawns further agents passes the
            type down.
          * ``parent_session_id`` -- the session that spawned the row. A row from a
            different session is that session's team, not ours.

        A row with neither field is a legacy entry written before ownership was
        recorded. It stays visible: the roster is a report, and hiding a real member
        is the worse error (``view`` already documents that rule for liveness).
        """
        agent_type = str(getattr(entry, "agent_type", "") or "").strip().lower()
        if agent_type == WORKFLOW_CHILD_AGENT_TYPE:
            return False
        parent_session = getattr(entry, "parent_session_id", None)
        if parent_session and self.session_id:
            return str(parent_session) == str(self.session_id)
        return True

    def members(self, include_closed=True):
        return [
            entry
            for entry in self.registry.list_agents(include_closed=include_closed)
            if self._is_team_member(entry)
        ]

    def view(self, include_closed=True):
        """Describe the team, including members that have already finished.

        ``include_closed`` defaults to True because the roster is a *report*, not a
        work queue. Callers that need "who is still alive" ask the registry directly with
        ``include_closed=False``; the panel that answers "how did my team do" must keep
        showing children after they end. Defaulting to False made the team look empty at
        the exact moment the user opens it to read the results: `manager.close_agent`
        marks every finished child ``status=closed``, so a finished session had zero
        visible members. Measured on real data: 11 of 11 registry entries were closed.

        How each member ended travels with it (`previousStatus` / `closedStatus`), so a
        closed row is still readable as "completed" rather than "not running".
        """
        members = []
        for entry in self.members(include_closed=include_closed):
            # Ask the registry whether the row still owns a live process. A child that
            # died without releasing keeps `status="running"` forever; without this probe
            # the panel shows it as `● running`. "Cannot tell" must stay alive: the roster
            # is a report, and calling a live agent dead is the worse mistake.
            live = True
            probe = getattr(self.registry, "is_live", None)
            if callable(probe):
                try:
                    live = bool(probe(entry.agent_path))
                except Exception:
                    live = True
            members.append(
                {
                    "agentId": str(entry.agent_path),
                    "name": entry.task_name,
                    "status": entry.status,
                    # The registry records the pre-close state and why it was closed. Without
                    # these, a finished member is indistinguishable from one that was never
                    # started: both would read as `closed`.
                    #
                    # `getattr` rather than attribute access so a duck-typed stand-in (tests
                    # pass plain objects; anything reading a registry written by an older
                    # build) still works. The roster is a report -- losing one field must not
                    # turn the whole panel into an error.
                    "previousStatus": getattr(entry, "previous_status", None),
                    "closedStatus": getattr(entry, "closed_status", None),
                    # A background teammate keeps `status="running"` between turns; its
                    # finished work only shows up here. Without it the panel paints a
                    # completed member as `● running` forever (24 of 80 real rows did).
                    "turnStatus": getattr(entry, "turn_status", None),
                    # Registry liveness, not the lifecycle word: a `running` row whose
                    # process is gone is a leftover, and the panel must not paint it as work.
                    "live": live,
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
