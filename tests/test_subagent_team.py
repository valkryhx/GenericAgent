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
    def __init__(self, agent_path, task_name, status="running", parent_path=None, run_id="run1"):
        self.agent_path = agent_path
        self.task_name = task_name
        self.status = status
        self.parent_path = parent_path
        self.run_id = run_id


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
        for name in ("main", "team-lead", "user", "system", "USER"):
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


if __name__ == "__main__":
    unittest.main()
