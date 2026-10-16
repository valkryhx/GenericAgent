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
