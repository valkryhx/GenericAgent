"""Name pins: a name must keep addressing the agent it first reached.

Agent names are reusable. ``close_agent(a)`` keeps the task dir and registry row, so
a later ``spawn_agent(a)`` gets ``a_1`` -- but a task dir that was removed (or a
different root) can hand the bare name to a *different* agent. Delivering to whoever
holds the name now is CC's explicitly-rejected "latest wins" behaviour; it is how
cross-talk starts in multi-agent systems.

CC evidence: ``sendMessagePins`` / ``hei()`` (``m1922:455-517``) decides
proceed-on-same / rebound-on-different / proceed-and-pin-on-first, keyed by name, with
a short stable ref so a refusal can name the agent the address used to reach.
"""

import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from genericagent.agents.manager import SubagentManager  # noqa: E402
from genericagent.agents.name_pins import (  # noqa: E402
    REF_LENGTH,
    NamePins,
    agent_ref,
    rebound_message,
)
from genericagent.agents.state import atomic_write_json  # noqa: E402


class AgentRefTest(unittest.TestCase):
    def test_ref_is_stable_and_short(self):
        first = agent_ref("/root/worker")
        self.assertEqual(first, agent_ref("/root/worker"))
        self.assertEqual(len(first), REF_LENGTH)
        self.assertRegex(first, r"^[0-9a-f]+$")

    def test_different_agents_get_different_refs(self):
        self.assertNotEqual(agent_ref("/root/worker"), agent_ref("/root/worker_1"))


class NamePinsTest(unittest.TestCase):
    def test_first_send_pins_and_the_same_agent_proceeds(self):
        with tempfile.TemporaryDirectory() as td:
            pins = NamePins(td)

            first = pins.resolve("worker", "/root/worker", run_id="run_000001")
            second = pins.resolve("worker", "/root/worker", run_id="run_000001")

            self.assertEqual(first["kind"], "proceed")
            self.assertEqual(second["kind"], "proceed")
            self.assertEqual(second["pin"]["ref"], agent_ref("/root/worker"))

    def test_a_different_agent_under_the_same_name_is_a_rebound(self):
        with tempfile.TemporaryDirectory() as td:
            pins = NamePins(td)
            pins.resolve("worker", "/root/worker", run_id="run_000001")

            result = pins.resolve("worker", "/root/worker_1", run_id="run_000002")

            self.assertEqual(result["kind"], "rebound")
            self.assertEqual(result["previous"]["agent_path"], "/root/worker")
            self.assertEqual(result["next"]["agent_path"], "/root/worker_1")

    def test_resume_under_a_new_run_id_is_not_a_rebound(self):
        """resume_agent relaunches the same agent; the path is the identity."""
        with tempfile.TemporaryDirectory() as td:
            pins = NamePins(td)
            pins.resolve("worker", "/root/worker", run_id="run_000001")

            result = pins.resolve("worker", "/root/worker", run_id="run_000007")

            self.assertEqual(result["kind"], "proceed")

    def test_pin_survives_and_is_not_overwritten_by_the_new_holder(self):
        with tempfile.TemporaryDirectory() as td:
            pins = NamePins(td)
            pins.resolve("worker", "/root/worker")

            pins.resolve("worker", "/root/worker_1")
            self.assertEqual(pins.get("worker")["agent_path"], "/root/worker")

    def test_clear_is_not_offered_on_close(self):
        """A pin must outlive the agent; deleting it restores the cross-talk bug."""
        self.assertFalse(hasattr(NamePins, "unpin"))
        self.assertFalse(hasattr(NamePins, "clear"))


class ReboundMessageTest(unittest.TestCase):
    def test_it_names_both_agents_and_the_next_step(self):
        message = rebound_message(
            {
                "name": "worker",
                "previous": {"agent_path": "/root/worker", "ref": "abcdef"},
                "next": {"agent_path": "/root/worker_1", "ref": "123456"},
            }
        )

        self.assertIn("/root/worker", message)
        self.assertIn("abcdef", message)
        self.assertIn("Nothing was sent", message)
        self.assertIn("/root/worker_1", message)
        self.assertIn("resume_agent", message)


class ManagerPinIntegrationTest(unittest.TestCase):
    """The guard has to run on the real delivery path, not just in NamePins."""

    def _manager(self, td):
        class FakeProcess:
            next_pid = 9400

            def __init__(self):
                self.pid = FakeProcess.next_pid
                FakeProcess.next_pid += 1

        return SubagentManager(
            root_dir=td,
            popen=lambda *_, **__: FakeProcess(),
            python_executable="python-test",
            sleep=lambda _: None,
            process_exists=lambda _pid: True,
        )

    def _write_agent_state(self, td, name, run_id):
        task_dir = Path(td) / "temp" / name
        task_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            task_dir / "state.json",
            {
                "schema_version": 1,
                "task_name": name,
                "agent_path": f"/root/{name}",
                "run_id": run_id,
                "pid": 4245,
                "round": 0,
                "turn_status": "running",
                "process_status": "alive",
            },
        )
        return task_dir

    def test_a_reused_name_is_refused_after_the_pin(self):
        with tempfile.TemporaryDirectory() as td:
            manager = self._manager(td)
            manager.spawn_agent("worker", "first")
            manager.send_message("worker", "hello", author="/root")

            # Simulate the name coming back around to a different agent: the pin says
            # "worker" is /root/worker, so a registry row for /root/worker_1 under the
            # bare name must not silently receive it.
            pins = NamePins(manager.subagents_state_dir)
            pins.resolve("worker", "/root/worker", run_id=None)

            result = pins.resolve("worker", "/root/worker_1", run_id="run_000099")
            self.assertEqual(result["kind"], "rebound")

    def test_the_first_send_to_a_fresh_name_still_goes_through(self):
        with tempfile.TemporaryDirectory() as td:
            manager = self._manager(td)
            manager.spawn_agent("worker", "first")

            row = manager.send_message("worker", "hello", author="/root")

            self.assertEqual(row["recipient"], "/root/worker")
            from genericagent.agents.mailbox import SubagentMailbox

            rows = SubagentMailbox(Path(td) / "temp" / "worker" / "mailbox.jsonl")._read_rows()
            self.assertIn("hello", [r["content"] for r in rows])

    def test_a_manager_that_cannot_resolve_the_agent_does_not_block_delivery(self):
        """The pin is a cross-talk guard, never a delivery precondition."""
        with tempfile.TemporaryDirectory() as td:
            manager = self._manager(td)
            self._write_agent_state(td, "loose", "run_000010")
            manager.registry.get = lambda *_: (_ for _ in ()).throw(OSError("registry unavailable"))

            row = manager.send_message("loose", "still lands", author="/root")

            self.assertEqual(row["recipient"], "/root/loose")


if __name__ == "__main__":
    unittest.main()
