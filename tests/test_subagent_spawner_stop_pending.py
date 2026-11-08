"""A spawner that is being shut down must not start new children.

The stop signal is observed *between* turns, so a tool call already in flight keeps
running. That means a stopped agent can still reach ``spawn_agent``, and the child it
starts is orphaned the moment the parent exits: nobody reads its output, and it holds
an active-agent slot until it dies on its own. CC guards this window with
``subagent_spawner_stop_pending`` (``m0795:910``).

In GA the stop signal is the durable ``_stop`` file in the spawner's own task dir --
the same one ``close_agent`` writes -- so the guard reads that rather than inventing a
second notion of "stopping".
"""

import sys
import tempfile
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from genericagent.agents.manager import SubagentManager  # noqa: E402


class _FakeProcess:
    next_pid = 9800

    def __init__(self):
        self.pid = _FakeProcess.next_pid
        _FakeProcess.next_pid += 1


def _manager(td, self_agent_path=None):
    return SubagentManager(
        root_dir=td,
        popen=lambda *_, **__: _FakeProcess(),
        python_executable="python-test",
        sleep=lambda _: None,
        process_exists=lambda _pid: True,
        self_agent_path=self_agent_path,
    )


class SpawnerStopPendingTest(unittest.TestCase):
    def test_root_is_never_refused(self):
        """Root has no task dir; a user who aborted and retypes must still be able to spawn."""
        with tempfile.TemporaryDirectory() as td:
            manager = _manager(td)

            handle = manager.spawn_agent("worker", "job")

            self.assertEqual(handle.task_name, "worker")

    def test_a_stopped_agent_cannot_spawn(self):
        with tempfile.TemporaryDirectory() as td:
            manager = _manager(td, self_agent_path="/root/mid_agent")
            stop_dir = Path(td) / "temp" / "mid_agent"
            stop_dir.mkdir(parents=True)
            (stop_dir / "_stop").write_text("parent_cleanup", encoding="utf-8")

            with self.assertRaises(ValueError) as ctx:
                manager.spawn_agent("child", "job")

            message = str(ctx.exception)
            self.assertIn("pending stop", message)
            self.assertIn("/root/mid_agent", message)
            self.assertIn("orphaned", message)

    def test_a_refused_spawn_starts_no_process_and_leaves_no_state(self):
        with tempfile.TemporaryDirectory() as td:
            manager = _manager(td, self_agent_path="/root/mid_agent")
            stop_dir = Path(td) / "temp" / "mid_agent"
            stop_dir.mkdir(parents=True)
            (stop_dir / "_stop").write_text("x", encoding="utf-8")

            with self.assertRaises(ValueError):
                manager.spawn_agent("child", "job")

            self.assertFalse((Path(td) / "temp" / "child" / "state.json").exists())

    def test_the_stop_file_is_not_consumed_by_the_guard(self):
        """Reading it to decide must not cancel the stop for the rest of the turn."""
        with tempfile.TemporaryDirectory() as td:
            manager = _manager(td, self_agent_path="/root/mid_agent")
            stop_dir = Path(td) / "temp" / "mid_agent"
            stop_dir.mkdir(parents=True)
            (stop_dir / "_stop").write_text("reason", encoding="utf-8")

            with self.assertRaises(ValueError):
                manager.spawn_agent("child", "job")

            self.assertTrue((stop_dir / "_stop").exists())
            self.assertEqual((stop_dir / "_stop").read_text(encoding="utf-8"), "reason")

    def test_an_agent_without_a_stop_file_spawns_normally(self):
        with tempfile.TemporaryDirectory() as td:
            manager = _manager(td, self_agent_path="/root/mid_agent")
            (Path(td) / "temp" / "mid_agent").mkdir(parents=True)

            handle = manager.spawn_agent("child", "job")

            self.assertEqual(handle.task_name, "child")

    def test_the_refusal_is_recorded_as_observable(self):
        with tempfile.TemporaryDirectory() as td:
            manager = _manager(td, self_agent_path="/root/mid_agent")
            stop_dir = Path(td) / "temp" / "mid_agent"
            stop_dir.mkdir(parents=True)
            (stop_dir / "_stop").write_text("x", encoding="utf-8")

            with self.assertRaises(ValueError):
                manager.spawn_agent("child", "job")

            events = manager.event_bus._read_jsonl(manager.event_bus.events_path)
            rejections = [e for e in events if e.get("type") == "spawn_rejected"]
            self.assertTrue(rejections, "a refused spawn must leave a trace")
            self.assertIn("pending stop", rejections[-1]["payload"]["reason"])


if __name__ == "__main__":
    unittest.main()
