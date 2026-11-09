"""Every child-side bus lookup must come from the hand-down, not from a path guess.

Real incident, same root cause as the earlier notification split: the spawner writes the
bus at ``<projectData>/<session>/subagents``, and hands the child ``GA_SUBAGENTS_STATE_DIR``
and ``GA_SESSION_DATA_DIR``. Seven child-side sites ignored both and re-derived
``dirname(task_dir)/subagents`` -- which is only correct for the legacy
``<stateRoot>/temp/<task>`` layout. On the product layout they all landed in a second,
orphaned directory under ``<bus>/tasks/``.

The worst of them was provenance: the lead records a digest in ``<bus>/provenance.jsonl``,
the child looked for it in ``<bus>/tasks/subagents/provenance.jsonl``, did not find it, saw
the author ``/root`` in the roster, returned ``forged``, and ``consume_trigger_turn``
dropped the message for good -- while the sender got a success receipt.

These tests build the *product* layout. The legacy layout is where the two paths coincide,
which is why every existing test stayed green while the product path was broken.
"""

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from genericagent.agents.event_bus import SubagentEventBus  # noqa: E402
from genericagent.agents.idle_subscriptions import IdleSubscriptions  # noqa: E402
from genericagent.agents.mailbox import SubagentMailbox  # noqa: E402
from genericagent.agents.manager import SubagentManager  # noqa: E402
from genericagent.agents.registry import SubagentRegistry  # noqa: E402
from genericagent.agents.receipts import ProvenanceLedger, build_roster  # noqa: E402
from genericagent.core import app as agentmain  # noqa: E402


def _product_manager(tmp, session="s1"):
    data = Path(tmp) / "data"
    project = Path(tmp) / "proj"
    project.mkdir(parents=True, exist_ok=True)
    return SubagentManager(
        project_root=project,
        project_state_root=data,
        project_data_dir=data / "projects" / "k",
        parent_session_id=session,
    )


class ChildBusHandDownTest(unittest.TestCase):
    """The two directories a child is told about, and nothing guessing at them."""

    def test_the_child_takes_the_bus_from_the_hand_down(self):
        with mock.patch.dict(os.environ, {"GA_SUBAGENTS_STATE_DIR": "C:/bus"}, clear=False):
            self.assertEqual("C:/bus", agentmain._subagent_bus_dir("C:/bus/tasks/kid"))

    def test_the_child_takes_the_session_dir_from_the_hand_down(self):
        """Sessions are not the bus: on the legacy layout they are siblings, not equals."""
        with mock.patch.dict(os.environ, {"GA_SESSION_DATA_DIR": "C:/sessions"}, clear=False):
            self.assertEqual("C:/sessions", agentmain._child_session_dir("C:/bus/tasks/kid"))

    def test_the_session_dir_is_not_silently_the_bus_dir(self):
        """A wrong "just reuse the bus" fix would pass a naive end-to-end check.

        On the *product* layout the session dir and the bus happen to be the same
        directory, so the only place the distinction is visible is the legacy layout --
        where sessions live beside the bus, not inside it. A fix that returned the bus
        for both would still pass a product-only test.
        """
        with tempfile.TemporaryDirectory() as tmp:
            legacy = SubagentManager(
                project_root=Path(tmp) / "proj", project_state_root=Path(tmp) / "proj"
            )
            self.assertNotEqual(
                Path(legacy.session_data_dir), Path(legacy.subagents_state_dir)
            )


class ChildTranscriptTest(unittest.TestCase):
    def test_transcripts_land_in_the_session_dir_not_under_tasks(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = _product_manager(tmp)
            task_dir = manager.task_root / "kid"
            task_dir.mkdir(parents=True, exist_ok=True)
            (task_dir / "state.json").write_text(
                json.dumps({"parent_session_id": "s1", "run_id": "run_1"}), encoding="utf-8"
            )

            with mock.patch.dict(
                os.environ,
                {
                    "GA_SESSION_DATA_DIR": str(manager.session_data_dir),
                    "GA_SUBAGENTS_STATE_DIR": str(manager.subagents_state_dir),
                },
                clear=False,
            ):
                agentmain._subagent_transcript_append(
                    str(task_dir), "turn_started", {"task_name": "kid"}
                )

            expected = Path(manager.session_data_dir) / "s1" / "subagents" / "run_1.jsonl"
            self.assertTrue(expected.exists(), "transcript did not land in the session dir")
            orphan = Path(manager.task_root) / "sessions"
            self.assertFalse(orphan.exists(), "transcript went to the orphan tasks/sessions dir")


class ChildRegistryReleaseTest(unittest.TestCase):
    def test_the_child_releases_its_slot_in_the_parent_registry(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = _product_manager(tmp)
            handle = manager.spawn_agent(
                "kid", "hi", permission_profile="inherit-current-permissions"
            ) if False else None
            # Register the row the way spawn does, without launching a process.
            task_dir = manager.task_root / "kid"
            task_dir.mkdir(parents=True, exist_ok=True)
            state = manager.register_agent("kid", {"pid": os.getpid()})
            (task_dir / "state.json").write_text(
                json.dumps({"agent_path": "/root/kid", "pid": os.getpid()}), encoding="utf-8"
            )

            with mock.patch.dict(
                os.environ,
                {"GA_SUBAGENTS_STATE_DIR": str(manager.subagents_state_dir)},
                clear=False,
            ):
                released = agentmain._release_subagent_slot(str(task_dir))

            self.assertIsNotNone(released, "the child could not find its own registry row")
            entry = SubagentRegistry(manager.subagents_state_dir).list_agents(include_closed=True)[0]
            self.assertEqual("closed", getattr(entry, "status", None))

    def test_a_self_releasing_child_returns_its_unfinished_tasks(self):
        """Releasing the slot must hand unfinished work back to the shared board.

        `manager.close_agent` reclaims owned tasks, but a child that ends by itself calls
        `release_agent_slot` directly and never passes through the manager. Real data
        (2026-11-09): one board task stayed `in_progress` with an owner whose registry row
        was already closed (`closed_status=released`).
        """
        from genericagent import paths
        from genericagent.agents.tasks import SubagentTaskStore

        with tempfile.TemporaryDirectory() as tmp:
            manager = _product_manager(tmp)
            roots = paths.project_roots(
                project=manager.project_root, data_root=manager.project_state_root
            )
            team_key = paths.team_key(roots.project_key, "default")
            store = SubagentTaskStore(
                roots.project_root,
                list_id="default",
                state_root=roots.data_root,
                tasks_root=roots.tasks_dir,
                team_key=team_key,
            )
            task = store.create_task("half-done work")
            store.claim_task(task["id"], "/root/kid", start=True)

            task_dir = manager.task_root / "kid"
            task_dir.mkdir(parents=True, exist_ok=True)
            manager.register_agent("kid", {"pid": os.getpid()})
            (task_dir / "state.json").write_text(
                json.dumps(
                    {
                        "agent_path": "/root/kid",
                        "pid": os.getpid(),
                        "parent_session_id": "s1",
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch.dict(
                os.environ,
                {
                    "GA_SUBAGENTS_STATE_DIR": str(manager.subagents_state_dir),
                    "GA_TASKS_ROOT": str(roots.tasks_dir),
                    "GA_TEAM_KEY": team_key,
                    "GA_PROJECT_ROOT": str(roots.project_root),
                    "GA_DATA_DIR": str(roots.data_root),
                    "GA_TEAM_NAME": "default",
                },
                clear=False,
            ):
                agentmain._release_subagent_slot(str(task_dir))

            row = store.get_task(task["id"])
            self.assertIsNone(row.get("owner"), "a released child still owns a task")
            self.assertEqual(
                "pending",
                row.get("status"),
                "unfinished work was not returned to the board when the child released its slot",
            )

    def test_no_second_registry_appears_under_the_task_root(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = _product_manager(tmp)
            task_dir = manager.task_root / "kid"
            task_dir.mkdir(parents=True, exist_ok=True)
            manager.register_agent("kid", {"pid": os.getpid()})
            (task_dir / "state.json").write_text(
                json.dumps({"agent_path": "/root/kid", "pid": os.getpid()}), encoding="utf-8"
            )

            with mock.patch.dict(
                os.environ,
                {"GA_SUBAGENTS_STATE_DIR": str(manager.subagents_state_dir)},
                clear=False,
            ):
                agentmain._release_subagent_slot(str(task_dir))

            self.assertFalse(
                (Path(manager.task_root) / "subagents").exists(),
                "an orphan registry was created under the task root",
            )


class ChildIdleSubscriptionTest(unittest.TestCase):
    def test_idle_ping_reaches_the_subscription_on_the_parent_bus(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = _product_manager(tmp)
            task_dir = manager.task_root / "kid"
            task_dir.mkdir(parents=True, exist_ok=True)
            (task_dir / "state.json").write_text(
                json.dumps({"parent_session_id": "s1", "run_id": "run_1"}), encoding="utf-8"
            )
            # The lead subscribes on the real bus; that is the only place it will read.
            IdleSubscriptions(manager.subagents_state_dir).subscribe("/root/kid")

            with mock.patch.dict(
                os.environ,
                {
                    "GA_SUBAGENTS_STATE_DIR": str(manager.subagents_state_dir),
                    "GA_SESSION_DATA_DIR": str(manager.session_data_dir),
                },
                clear=False,
            ):
                agentmain._teammate_idle_hook(str(task_dir), "kid")

            self.assertEqual(
                [],
                IdleSubscriptions(manager.subagents_state_dir).list_paths(),
                "the subscription was never consumed: the child looked elsewhere",
            )
            events = SubagentEventBus(manager.subagents_state_dir).read_events_since(0)
            self.assertTrue(
                any(e.get("type") == "idle_notification" for e in events),
                "no idle_notification on the bus the lead reads",
            )
            self.assertFalse(
                (Path(manager.task_root) / "subagents").exists(),
                "an orphan bus was created under the task root",
            )


class ChildProvenanceTest(unittest.TestCase):
    """The one that loses a message outright."""

    def test_a_message_from_the_lead_is_delivered_not_marked_forged(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = _product_manager(tmp)
            task_dir = manager.task_root / "kid"
            task_dir.mkdir(parents=True, exist_ok=True)

            SubagentMailbox(task_dir / "mailbox.jsonl").enqueue(
                "please do the thing",
                author="/root",
                recipient="/root/kid",
                delivery_mode="trigger_turn",
                message_id="msg_test_0001",
                source_tool="followup_task",
                provenance=manager.provenance_ledger,
            )

            with mock.patch.dict(
                os.environ,
                {
                    "GA_SUBAGENTS_STATE_DIR": str(manager.subagents_state_dir),
                    "GA_SESSION_DATA_DIR": str(manager.session_data_dir),
                },
                clear=False,
            ):
                ledger, roster = agentmain._child_provenance(str(task_dir))

            result = SubagentMailbox(task_dir / "mailbox.jsonl").consume_trigger_turn(
                provenance=ledger, roster=roster
            )

            self.assertIsNotNone(result, "the lead's message was refused as forged")
            self.assertIn("please do the thing", result.get("content") or "")
            rows = SubagentMailbox(task_dir / "mailbox.jsonl")._read_rows()
            self.assertNotEqual("forged", rows[0].get("state"))

    def test_the_roster_sees_live_teammates(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = _product_manager(tmp)
            manager.register_agent("kid", {"pid": os.getpid()})

            with mock.patch.dict(
                os.environ,
                {"GA_SUBAGENTS_STATE_DIR": str(manager.subagents_state_dir)},
                clear=False,
            ):
                _, roster = agentmain._child_provenance(str(manager.task_root / "kid"))

            self.assertIn("/root/kid", roster)


class NoPathGuessRemainsTest(unittest.TestCase):
    """Guard: this defect has recurred three times. Make the shape unwritable."""

    def test_app_py_does_not_derive_a_bus_from_the_task_dir(self):
        """No live code path may rebuild a bus/session path from the task dir.

        Scoped with AST rather than by text so the guard means exactly what the defect
        was: an ``os.path.join(os.path.dirname(task_dir), ...)`` that builds a *sibling
        directory* (``subagents`` / ``sessions``). Two things are deliberately allowed:

        * the two helpers' own fallbacks, which exist for a child launched without a
          hand-down (legacy callers and unit tests);
        * ``dirname(dirname(task_dir))``, which is the explicitly-labelled legacy
          task-board root and not a bus path.

        An earlier version rebuilt the source one token per line, so the joined
        expression never appeared and the guard passed against code that still
        contained all seven offenders.
        """
        import ast

        source = (REPO_ROOT / "genericagent" / "core" / "app.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        allowed = {"_subagent_bus_dir", "_child_session_dir"}

        def is_dirname_of_task_dir(node):
            return (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "dirname"
                and node.args
                and isinstance(node.args[0], ast.Name)
                and node.args[0].id == "task_dir"
            )

        offenders = []
        for func in [n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)]:
            if func.name in allowed:
                continue
            for node in ast.walk(func):
                if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
                    continue
                if node.func.attr != "join":
                    continue
                if any(is_dirname_of_task_dir(arg) for arg in node.args):
                    offenders.append("%s:%s" % (func.name, node.lineno))

        self.assertEqual([], offenders, "bus derived from the task dir again")

    def test_the_helpers_exist(self):
        self.assertTrue(callable(getattr(agentmain, "_child_session_dir", None)))
        self.assertTrue(callable(getattr(agentmain, "_child_provenance", None)))


if __name__ == "__main__":
    unittest.main()
