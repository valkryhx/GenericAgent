import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from genericagent.core.loop import exhaust  # noqa: E402
from genericagent.core.handler import GenericAgentHandler  # noqa: E402
from genericagent.agents.manager import SubagentManager, normalize_task_name  # noqa: E402
from genericagent.agents.registry import SubagentRegistry  # noqa: E402
from genericagent.agents.team import (  # noqa: E402
    LEAD_AGENT_NAME,
    SubagentTeam,
    is_reserved_name,
    team_name_for,
)


class _Entry:
    def __init__(
        self,
        agent_path,
        task_name,
        status="running",
        parent_path=None,
        run_id="run1",
        turn_status=None,
        process_status=None,
        parent_session_id=None,
        agent_type=None,
    ):
        self.agent_path = agent_path
        self.task_name = task_name
        self.status = status
        self.parent_path = parent_path
        self.run_id = run_id
        self.turn_status = turn_status
        self.process_status = process_status
        # Ownership fields. A row belongs to the team that spawned it; without them the
        # roster cannot tell a teammate from a workflow child (`team.py:members`).
        self.parent_session_id = parent_session_id
        self.agent_type = agent_type


class _FakeRegistry:
    def __init__(self, entries):
        self._entries = entries

    def list_agents(self, path_prefix=None, include_closed=False):
        return list(self._entries)


class SubagentTeamTest(unittest.TestCase):
    def test_team_name_derivation(self):
        self.assertEqual(team_name_for(None), "default")
        self.assertEqual(team_name_for("abcdef1234567890"), "session-abcdef12")
        self.assertEqual(team_name_for("abcdef1234567890", env={"GA_TEAM_NAME": "alpha"}), "alpha")
        # GA session ids are ``session_<uuid4hex>``; the constant ``session_`` prefix
        # must not be what gets sliced, or every session shares one team name.
        self.assertEqual(team_name_for("session_a47239e44b0f42eabd6b3cee0f193ef2"), "session-a47239e4")

    def test_reserved_names(self):
        for name in ("main", "team-lead", "user", "system", "USER", "root", "ROOT"):
            self.assertTrue(is_reserved_name(name), name)
        self.assertFalse(is_reserved_name("researcher"))
        self.assertFalse(is_reserved_name(""))

    def test_validate_name_rejects_reserved(self):
        with self.assertRaises(ValueError):
            SubagentTeam.validate_name("team-lead")
        self.assertEqual(SubagentTeam.validate_name("researcher"), "researcher")

    def test_normalize_task_name_rejects_reserved(self):
        with self.assertRaises(ValueError):
            normalize_task_name("main")
        self.assertEqual(normalize_task_name("worker_1"), "worker_1")

    def test_view_derives_members(self):
        registry = _FakeRegistry(
            [
                _Entry("/root/alpha", "alpha"),
                _Entry("/root/beta", "beta", status="closed"),
            ]
        )
        team = SubagentTeam(registry, "/root", team_name="session-abc")
        view = team.view()
        self.assertEqual(view["teamName"], "session-abc")
        self.assertEqual(view["leadName"], LEAD_AGENT_NAME)
        self.assertEqual(view["leadAgentId"], "/root")
        self.assertEqual([m["name"] for m in view["members"]], ["alpha", "beta"])
        self.assertEqual(view["memberCount"], 2)


class TeamRosterLivenessTest(unittest.TestCase):
    """The roster must keep showing members after they finish.

    A finished teammate is closed by `manager.close_agent`, which writes
    `status="closed"` through `registry.mark_closed`. `list_agents(include_closed=False)`
    filters exactly those rows, so a roster read with the default argument returns nothing
    for any session that has completed -- and `/team` is opened precisely to look at a
    finished team.

    Measured before the fix on real data (7 registry files, 11 entries): every row was
    `status=closed`, so `view()` showed 0 members for every finished session.
    """

    def _team(self, root: Path) -> tuple[SubagentRegistry, SubagentTeam]:
        registry = SubagentRegistry(root)
        for name in ("alpha", "bravo", "charlie"):
            task_dir = root / name
            task_dir.mkdir(parents=True, exist_ok=True)
            registry.create_child("/root", name, task_dir, task_dir / "state.json")
        return registry, SubagentTeam(registry, "/root", team_name="session-test")

    def test_finished_members_stay_in_the_roster(self):
        with tempfile.TemporaryDirectory() as tmp:
            registry, team = self._team(Path(tmp))
            self.assertEqual(team.view()["memberCount"], 3, "three live teammates")

            for name in ("alpha", "bravo", "charlie"):
                registry.mark_closed(f"/root/{name}", previous_status="completed", closed_status="shutdown")

            view = team.view()
            self.assertEqual(
                view["memberCount"],
                3,
                "finished members vanished from the roster the moment they completed",
            )
            self.assertEqual({m["name"] for m in view["members"]}, {"alpha", "bravo", "charlie"})

    def test_closed_members_carry_the_verdict_that_ended_them(self):
        # `status` is "closed" for every finished member, so without these two fields a
        # member whose work completed and one that errored are indistinguishable.
        with tempfile.TemporaryDirectory() as tmp:
            registry, team = self._team(Path(tmp))
            registry.mark_closed("/root/alpha", previous_status="completed", closed_status="shutdown")
            registry.mark_closed("/root/bravo", previous_status="errored", closed_status="killed")
            registry.mark_closed("/root/charlie", previous_status="interrupted", closed_status="shutdown")

            by_name = {m["name"]: m for m in team.view()["members"]}
            self.assertEqual(by_name["alpha"]["previousStatus"], "completed")
            self.assertEqual(by_name["bravo"]["previousStatus"], "errored")
            self.assertEqual(by_name["charlie"]["previousStatus"], "interrupted")
            self.assertEqual(by_name["bravo"]["closedStatus"], "killed")

    def test_a_duck_typed_entry_without_the_new_fields_still_renders(self):
        # The roster is a report: one missing field must not turn the whole panel into an
        # error. `_Entry` here deliberately lacks previous_status/closed_status.
        team = SubagentTeam(_FakeRegistry([_Entry("/root/alpha", "alpha", status="closed")]), "/root")
        view = team.view()
        self.assertEqual(view["memberCount"], 1)
        self.assertIsNone(view["members"][0]["previousStatus"])


class TeamRosterWorkVerdictTest(unittest.TestCase):
    """A live row whose turn already ended must expose that turn's verdict.

    A background teammate stays alive between turns: the registry holds
    ``status="running"`` with ``turn_status="completed"`` and
    ``process_status="waiting_reply"``. Measured on real data: 24 of 80 rows sat in
    exactly that state. The roster only exposed ``status``, so ``/team`` painted them
    as ``● running`` forever -- indistinguishable from a member that never started,
    and blind to the same lifecycle-word trap the subagent announcements already fixed.
    """

    def test_a_live_member_reports_its_finished_turn(self):
        team = SubagentTeam(
            _FakeRegistry([_Entry("/root/alpha", "alpha", status="running", turn_status="completed")]),
            "/root",
        )
        member = team.view()["members"][0]
        self.assertEqual(member["status"], "running")
        self.assertEqual(member["turnStatus"], "completed")

    def test_closed_members_also_carry_the_turn_status(self):
        team = SubagentTeam(
            _FakeRegistry(
                [
                    _Entry(
                        "/root/alpha",
                        "alpha",
                        status="closed",
                        turn_status="completed",
                    )
                ]
            ),
            "/root",
        )
        self.assertEqual(team.view()["members"][0]["turnStatus"], "completed")


class _LiveRegistry(_FakeRegistry):
    """Registry stand-in that also answers the liveness probe."""

    def __init__(self, entries, live):
        super().__init__(entries)
        self._live = live

    def is_live(self, agent_path):
        return bool(self._live.get(str(agent_path), True))


class TeamRosterLivenessProjectionTest(unittest.TestCase):
    """The roster must ask the registry whether a running row still owns a live process.

    Real data (2026-11-09 audit): all 29 rows still marked `status=running` across the
    machine were judged dead by the registry's own liveness probe (pid gone or identity
    mismatch). `/team` painted every one of them as `● running`, so an abandoned session
    looked like a team hard at work. The registry already owns the answer; the read path
    must ask it instead of trusting the lifecycle word.
    """

    def test_a_dead_running_row_is_marked_not_live(self):
        registry = _LiveRegistry(
            [_Entry("/root/alpha", "alpha", status="running")], {"/root/alpha": False}
        )
        member = SubagentTeam(registry, "/root").view()["members"][0]
        self.assertFalse(member["live"])

    def test_a_live_running_row_stays_live(self):
        registry = _LiveRegistry(
            [_Entry("/root/alpha", "alpha", status="running")], {"/root/alpha": True}
        )
        self.assertTrue(SubagentTeam(registry, "/root").view()["members"][0]["live"])

    def test_a_registry_without_a_probe_does_not_invent_dead_members(self):
        # The roster is a report: "cannot tell" must render as alive, never as dead.
        member = SubagentTeam(_FakeRegistry([_Entry("/root/alpha", "alpha")]), "/root").view()["members"][0]
        self.assertTrue(member["live"])


class FakeParent:
    def __init__(self):
        self.llmclient = None
        self.verbose = False
        self.task_dir = None
        self.llm_no = 0


class TeamStatusToolTest(unittest.TestCase):
    def test_team_status_tool(self):
        with tempfile.TemporaryDirectory() as td:
            handler = GenericAgentHandler(FakeParent(), last_history=[], cwd=str(Path(td) / "temp"))
            handler.subagent_manager = SubagentManager(root_dir=td)
            outcome = exhaust(handler.do_team_status({}, response=None))
            self.assertEqual(outcome.data["status"], "success")
            self.assertEqual(outcome.data["team"]["teamName"], "default")
            self.assertEqual(outcome.data["team"]["leadName"], "team-lead")


class TeamMembershipScopeTest(unittest.TestCase):
    """The team roster must list *this team's* members, not the whole registry.

    Reported from a real run: a workflow was started with 3 subagents, and while it
    was still running the agent-team overlay listed those workflow children as team
    members -- their row names carried the workflow runId
    (``impl-c  wf_1778661927543-21c-1``), and their task rows read ``[working]``
    while the roster said every member was idle.

    Cause: ``SubagentTeam.members()`` forwarded straight to
    ``SubagentRegistry.list_agents()`` with no prefix and no ownership filter, so
    every row in the shared registry became a "teammate": workflow children, other
    sessions' leftovers, rescues -- anything that had ever been spawned.

    Ownership comes from two fields the registry already records:

      * ``parent_session_id`` -- the session that spawned the row;
      * ``agent_type``        -- ``workflow_child`` for children a workflow owns.

    A row belongs to the team when it was spawned by this session's lead. Rows
    spawned by a workflow run are that run's children, not the team's.
    """

    def _registry(self, entries, *, team_name="session-abc"):
        return _FakeRegistry(entries)

    def test_workflow_children_are_not_team_members(self):
        from genericagent.agents.team import SubagentTeam

        registry = _FakeRegistry([
            _Entry("/root/alpha", "alpha"),
            _Entry("/root/beta", "beta", agent_type="workflow_child"),
        ])
        team = SubagentTeam(registry, "/root", team_name="session-abc")
        names = [m["name"] for m in team.view()["members"]]
        self.assertEqual(
            ["alpha"],
            names,
            f"a workflow child was listed as a team member: {names}",
        )

    def test_members_from_another_session_are_not_team_members(self):
        from genericagent.agents.team import SubagentTeam

        registry = _FakeRegistry([
            _Entry("/root/mine", "mine", parent_session_id="session-abc"),
            _Entry("/root/theirs", "theirs", parent_session_id="session-other"),
        ])
        team = SubagentTeam(registry, "/root", team_name="session-abc", session_id="session-abc")
        names = [m["name"] for m in team.view()["members"]]
        self.assertEqual(["mine"], names, f"another session's agent joined this team: {names}")

    def test_without_a_session_id_only_the_workflow_filter_applies(self):
        """Callers that know just the team name still get the workflow-child filter,
        but cannot tell another session's row apart -- same as before this fix."""
        from genericagent.agents.team import SubagentTeam

        registry = _FakeRegistry([
            _Entry("/root/alpha", "alpha"),
            _Entry("/root/wfkid", "wfkid", agent_type="workflow_child"),
        ])
        team = SubagentTeam(registry, "/root", team_name="session-abc")
        names = [m["name"] for m in team.view()["members"]]
        self.assertEqual(["alpha"], names, f"workflow child leaked without a session id: {names}")

    def test_workflow_nested_rows_are_excluded_even_without_the_type(self):
        """A workflow child can itself spawn; those grandchildren carry the workflow's
        session, and the type is the only surviving marker after a restart."""
        from genericagent.agents.team import SubagentTeam

        registry = _FakeRegistry([
            _Entry("/root/alpha", "alpha"),
            _Entry("/root/alpha/inner", "inner", agent_type="workflow_child"),
        ])
        team = SubagentTeam(registry, "/root", team_name="session-abc")
        names = [m["name"] for m in team.view()["members"]]
        self.assertNotIn("inner", names, f"a nested workflow agent was listed: {names}")

    def test_a_row_with_no_ownership_fields_stays_visible(self):
        """Legacy rows predate these fields. Dropping them would hide real members,
        which is the worse failure: the roster is a report, not a permission check."""
        from genericagent.agents.team import SubagentTeam

        registry = _FakeRegistry([
            _Entry("/root/legacy", "legacy"),
        ])
        team = SubagentTeam(registry, "/root", team_name="session-abc")
        names = [m["name"] for m in team.view()["members"]]
        self.assertEqual(["legacy"], names, "a legacy row without ownership fields was hidden")

    def test_the_lead_itself_still_counted_before_still_counts(self):
        """Existing behaviour must not regress: plain teammates are unaffected."""
        from genericagent.agents.team import SubagentTeam

        registry = _FakeRegistry([
            _Entry("/root/alpha", "alpha", parent_session_id="session-abc"),
            _Entry("/root/beta", "beta", status="closed", parent_session_id="session-abc"),
        ])
        team = SubagentTeam(registry, "/root", team_name="session-abc")
        self.assertEqual(
            ["alpha", "beta"],
            [m["name"] for m in team.view()["members"]],
            "a closed teammate (or a running one) was dropped from the roster",
        )


if __name__ == "__main__":
    unittest.main()
