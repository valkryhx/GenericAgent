"""The notification bus has one directory, and every party resolves it the same way.

Real incident (found by running a real child, not by a test): a spawned subagent finished
its turn, and the main agent -- sitting in a 36-second tool loop the whole time -- never
learned. The notification went to ``<projectData>/<session>/subagents`` while the main
agent drained ``<projectRoot>/temp/subagents``. Both directories existed, both looked
healthy, and the reader simply found an empty queue.

The reason no test caught it: on the legacy layout the two paths coincide. Every existing
unit test builds the manager the legacy way, so the reader and writer agreed by accident.
These tests build the *product* layout, where they did not.

Three parties share this bus:
  * the manager that spawns children and mirrors their events
  * the main agent, draining at a fold point and at a turn start
  * the child, mirroring its own turn events back
"""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from genericagent.agents.event_bus import SubagentEventBus  # noqa: E402
from genericagent.agents.manager import SubagentManager  # noqa: E402
from genericagent.agents.notifications import (  # noqa: E402
    peek_subagent_notifications_prompt,
    resolve_notification_bus_dir,
)


class ResolveDirsTest(unittest.TestCase):
    """One function decides the layout; both sides must use it."""

    def test_project_bucket_layout(self):
        session, tasks, bus = SubagentManager.resolve_dirs(
            project_root="D:/proj",
            project_state_root="C:/data",
            project_data_dir="C:/data/projects/k",
            parent_session_id="s1",
        )

        self.assertEqual(Path("C:/data/projects/k/s1/subagents"), bus)
        self.assertEqual(bus, session)
        self.assertEqual(bus / "tasks", tasks)

    def test_legacy_layout_is_unchanged(self):
        """Existing callers keep their exact historical paths."""
        session, tasks, bus = SubagentManager.resolve_dirs(
            project_root="D:/proj", project_state_root="D:/proj"
        )

        self.assertEqual(Path("D:/proj/temp/subagents"), bus)
        self.assertEqual(Path("D:/proj/temp"), tasks)
        self.assertEqual(Path("D:/proj/temp/sessions"), session)

    def test_a_project_bucket_without_a_session_has_no_session_level(self):
        session, tasks, bus = SubagentManager.resolve_dirs(
            project_root="D:/proj",
            project_state_root="C:/data",
            project_data_dir="C:/data/projects/k",
        )

        self.assertEqual(Path("C:/data/projects/k/subagents"), bus)
        self.assertEqual(Path("C:/data/projects/k"), tasks)
        self.assertEqual(Path("C:/data/projects/k"), session)


class ManagerUsesResolveDirsTest(unittest.TestCase):
    def test_the_manager_takes_its_layout_from_the_shared_resolver(self):
        manager = SubagentManager(
            project_root="D:/proj",
            project_state_root="C:/data",
            project_data_dir="C:/data/projects/k",
            parent_session_id="s1",
        )

        self.assertEqual(Path("C:/data/projects/k/s1/subagents"), manager.subagents_state_dir)
        self.assertEqual(Path("C:/data/projects/k/s1/subagents/tasks"), manager.task_root)

    def test_a_child_is_told_where_the_bus_is(self):
        """The child cannot derive it: its task dir differs per layout."""
        manager = SubagentManager(
            project_root="D:/proj",
            project_state_root="C:/data",
            project_data_dir="C:/data/projects/k",
            parent_session_id="s1",
        )
        env_seen = {}

        class _Proc:
            pid = 1

        def popen(cmd, **kwargs):
            env_seen.update(kwargs.get("env") or {})
            return _Proc()

        manager.popen = popen
        manager.spawn_agent(
            "kid",
            "hi",
            parent_session_id="s1",
            permission_profile="inherit-current-permissions",
        )

        self.assertEqual(str(manager.subagents_state_dir), env_seen.get("GA_SUBAGENTS_STATE_DIR"))


class ReaderAgreesWithWriterTest(unittest.TestCase):
    def test_the_reader_resolves_the_project_bucket(self):
        with mock.patch("genericagent.paths.project_roots") as roots, mock.patch(
            "genericagent.paths.data_root", return_value="C:/data"
        ):
            roots.return_value = mock.MagicMock(
                project_data_dir=Path("C:/data/projects/k"), data_root=Path("C:/data")
            )
            resolved = resolve_notification_bus_dir(
                "D:/proj", data_root="C:/data", session_id="s1"
            )

        self.assertEqual(Path("C:/data/projects/k/s1/subagents"), Path(resolved))

    def test_what_the_manager_writes_is_what_the_reader_sees(self):
        """End to end on the product layout -- the shape that was broken."""
        with mock.patch("genericagent.paths.project_roots") as roots:
            roots.return_value = mock.MagicMock(
                project_data_dir=Path("C:/data/projects/k"), data_root=Path("C:/data")
            )
            manager = SubagentManager(
                project_root="D:/proj",
                project_state_root="C:/data",
                project_data_dir="C:/data/projects/k",
                parent_session_id="s1",
            )
            bus = Path(manager.subagents_state_dir)
            bus.mkdir(parents=True, exist_ok=True)
            SubagentEventBus(bus).append_event(
                "turn_completed",
                agent_path="/root/kid",
                run_id="run_000001",
                task_name="kid",
                notify=True,
            )

            # The reader derives the path from the agent's identity, not from a formula
            # duplicated in the test.
            text, ack = peek_subagent_notifications_prompt(
                "D:/proj", data_root="C:/data", session_id="s1"
            )

        self.assertIn("[GA_SUBAGENT_NOTIFICATIONS]", text)
        self.assertIn("/root/kid", text)
        self.assertGreater(ack or 0, 0)

    def test_a_bare_project_root_does_not_silently_read_the_legacy_dir(self):
        """The exact failure: right-looking path, wrong directory, empty queue."""
        with mock.patch("genericagent.paths.project_roots") as roots:
            roots.return_value = mock.MagicMock(
                project_data_dir=Path("C:/data/projects/k"), data_root=Path("C:/data")
            )
            manager = SubagentManager(
                project_root="D:/proj",
                project_state_root="C:/data",
                project_data_dir="C:/data/projects/k",
                parent_session_id="s1",
            )
            Path(manager.subagents_state_dir).mkdir(parents=True, exist_ok=True)
            SubagentEventBus(Path(manager.subagents_state_dir)).append_event(
                "turn_completed", agent_path="/root/kid", task_name="kid", notify=True
            )
            text, _ = peek_subagent_notifications_prompt(
                "D:/proj", data_root="C:/data", session_id="s1"
            )

        legacy = Path("D:/proj/temp/subagents")
        self.assertNotEqual(legacy, Path(manager.subagents_state_dir))
        self.assertIn("[GA_SUBAGENT_NOTIFICATIONS]", text, "reader still used the legacy dir")


class PromptSectionUsesTheSessionTest(unittest.TestCase):
    def test_turn_start_drain_passes_the_agent_identity(self):
        """A bare project_root here was the second half of the same bug."""
        from genericagent.core import app as agentmain

        captured = {}

        def fake(project_root, **kwargs):
            captured["root"] = project_root
            captured.update(kwargs)
            return ""

        agent = mock.MagicMock()
        agent.project_root = "D:/proj"
        agent.data_root = "C:/data"
        agent.session_id = "s1"
        agent.task_dir = None
        agent.tool_cwd = "D:/proj"

        with mock.patch(
            "genericagent.agents.notifications.build_subagent_notifications_prompt", side_effect=fake
        ):
            agentmain.build_system_prompt_sections(agent)

        self.assertEqual("C:/data", captured.get("data_root"))
        self.assertEqual("s1", captured.get("session_id"))


class ChildMirrorsIntoTheParentBusTest(unittest.TestCase):
    def test_the_child_honours_the_handed_down_bus_dir(self):
        from genericagent.core import app as agentmain

        with mock.patch.dict(os.environ, {"GA_SUBAGENTS_STATE_DIR": "C:/bus"}, clear=False):
            self.assertEqual("C:/bus", agentmain._subagent_bus_dir("D:/x/temp/kid"))

    def test_without_it_the_child_falls_back_to_the_legacy_guess(self):
        from genericagent.core import app as agentmain

        env = {k: v for k, v in os.environ.items() if k != "GA_SUBAGENTS_STATE_DIR"}
        with mock.patch.dict(os.environ, env, clear=True):
            got = agentmain._subagent_bus_dir(os.path.join("D:", os.sep, "x", "temp", "kid"))

        self.assertEqual(
            Path(os.path.join("D:", os.sep, "x", "temp", "subagents")),
            Path(got),
        )


if __name__ == "__main__":
    unittest.main()
