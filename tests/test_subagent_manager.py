import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from subagent_state import append_jsonl_event, atomic_write_json, read_json_or_none  # noqa: E402
from subagent_manager import SubagentManager  # noqa: E402


class SubagentManagerReadTest(unittest.TestCase):
    def _write_running_state(self, root, task_name="wc_france_history", pid=1234):
        task_dir = Path(root) / "temp" / task_name
        task_dir.mkdir(parents=True)
        output_path = task_dir / "output.txt"
        output_path.write_text("France final answer\n\n[ROUND END]\n", encoding="utf-8")
        atomic_write_json(
            task_dir / "state.json",
            {
                "schema_version": 1,
                "task_name": task_name,
                "agent_path": f"/root/{task_name}",
                "pid": pid,
                "round": 0,
                "turn_status": "running",
                "process_status": "alive",
                "output_path": str(output_path),
                "final_output_path": None,
            },
        )
        return task_dir

    def test_read_agent_treats_round_end_as_completed_even_when_process_is_alive(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = self._write_running_state(td)
            manager = SubagentManager(root_dir=td, process_exists=lambda pid: pid == 1234)

            state = manager.read_agent("wc_france_history")

            self.assertEqual(state.turn_status, "completed")
            self.assertEqual(state.process_status, "waiting_reply")
            self.assertEqual(Path(state.final_output_path), task_dir / "output.txt")
            persisted = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(persisted["turn_status"], "completed")
            self.assertEqual(persisted["process_status"], "waiting_reply")

    def test_read_agent_does_not_treat_partial_output_as_final(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td) / "temp" / "wc_brazil_history"
            task_dir.mkdir(parents=True)
            output_path = task_dir / "output.txt"
            output_path.write_text("Brazil partial answer without final marker", encoding="utf-8")
            atomic_write_json(
                task_dir / "state.json",
                {
                    "schema_version": 1,
                    "task_name": "wc_brazil_history",
                    "agent_path": "/root/wc_brazil_history",
                    "pid": 4321,
                    "round": 0,
                    "turn_status": "running",
                    "process_status": "alive",
                    "output_path": str(output_path),
                    "final_output_path": None,
                },
            )
            manager = SubagentManager(root_dir=td, process_exists=lambda pid: pid == 4321)

            state = manager.read_agent("wc_brazil_history")

            self.assertEqual(state.turn_status, "running")
            self.assertEqual(state.process_status, "alive")
            self.assertIsNone(state.final_output_path)

    def test_read_agent_does_not_use_previous_round_output_to_complete_current_round(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td) / "temp" / "multi_round"
            task_dir.mkdir(parents=True)
            previous_output = task_dir / "output.txt"
            previous_output.write_text("first round\n\n[ROUND END]\n", encoding="utf-8")
            next_output = task_dir / "output1.txt"
            atomic_write_json(
                task_dir / "state.json",
                {
                    "schema_version": 1,
                    "task_name": "multi_round",
                    "agent_path": "/root/multi_round",
                    "pid": 2222,
                    "round": 1,
                    "turn_status": "running",
                    "process_status": "alive",
                    "output_path": str(next_output),
                    "final_output_path": str(previous_output),
                },
            )
            manager = SubagentManager(root_dir=td, process_exists=lambda pid: pid == 2222)

            state = manager.read_agent("multi_round")

            self.assertEqual(state.round, 1)
            self.assertEqual(state.turn_status, "running")
            self.assertEqual(state.process_status, "alive")
            self.assertEqual(Path(state.output_path), next_output)
            self.assertEqual(Path(state.final_output_path), previous_output)

    def test_close_agent_rejects_root_path(self):
        with tempfile.TemporaryDirectory() as td:
            manager = SubagentManager(root_dir=td)

            with self.assertRaises(ValueError):
                manager.close_agent("/root")

    def test_foreground_background_handoff_updates_state_and_events(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = self._write_running_state(td, task_name="handoff_worker", pid=None)
            state_path = task_dir / "state.json"
            raw = json.loads(state_path.read_text(encoding="utf-8"))
            raw.update({"background": True, "run_id": "run_handoff", "parent_session_id": "session_handoff"})
            atomic_write_json(state_path, raw)
            manager = SubagentManager(root_dir=td, process_exists=lambda _pid: False, sleep=lambda _: None)

            foreground = manager.request_foreground("handoff_worker", reason="inspect live output")
            background = manager.request_background("handoff_worker", reason="return to queue")

            self.assertTrue(foreground.previous_state.background)
            self.assertFalse(foreground.updated_state.background)
            self.assertEqual(foreground.handoff_mode, "foreground")
            self.assertFalse(background.previous_state.background)
            self.assertTrue(background.updated_state.background)
            self.assertEqual(background.handoff_mode, "background")
            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertTrue(persisted["background"])
            self.assertEqual(persisted["handoff_mode"], "background")
            self.assertEqual(persisted["handoff_reason"], "return to queue")
            events = [json.loads(line) for line in (task_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(events[-2]["type"], "foreground_requested")
            self.assertEqual(events[-1]["type"], "background_requested")
            self.assertEqual(events[-1]["reason"], "return to queue")

    def test_attach_agent_streams_live_output_tail_and_detach_returns_to_background(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = self._write_running_state(td, task_name="attach_worker", pid=None)
            state_path = task_dir / "state.json"
            raw = json.loads(state_path.read_text(encoding="utf-8"))
            raw.update({"background": True, "run_id": "run_attach", "parent_session_id": "session_attach"})
            atomic_write_json(state_path, raw)
            output_path = task_dir / "output.txt"
            output_path.write_text("partial live chunk one\n", encoding="utf-8")
            manager = SubagentManager(root_dir=td, process_exists=lambda _pid: False, sleep=lambda _: None)

            first = manager.attach_agent("attach_worker", max_chars=10, reason="watch live output")

            self.assertEqual(first.handoff_mode, "foreground")
            self.assertEqual(first.attach_status, "attached")
            self.assertFalse(first.state.background)
            self.assertEqual(first.stream_text, "partial li")
            self.assertTrue(first.stream_truncated)
            self.assertEqual(first.stream_offset, 0)
            self.assertEqual(first.next_stream_offset, 10)
            self.assertFalse(first.stream_eof)
            self.assertIsNotNone(first.next_event_seq)

            output_path.write_text("partial live chunk one\nsecond chunk\n\n[ROUND END]\n", encoding="utf-8")
            second = manager.attach_agent("attach_worker", since_offset=first.next_stream_offset)

            self.assertEqual(second.stream_text, "ve chunk one\nsecond chunk\n\n[ROUND END]\n")
            self.assertFalse(second.stream_truncated)
            self.assertTrue(second.stream_eof)
            self.assertEqual(second.next_stream_offset, len(output_path.read_text(encoding="utf-8")))

            detached = manager.detach_agent("attach_worker", reason="return to queue")

            self.assertEqual(detached.handoff_mode, "background")
            self.assertEqual(detached.attach_status, "detached")
            self.assertTrue(detached.state.background)
            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(persisted["attach_status"], "detached")
            self.assertTrue(persisted["background"])
            types = [json.loads(line)["type"] for line in (task_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(types[-3:], ["foreground_requested", "foreground_requested", "background_requested"])

    def test_close_agent_returns_previous_state_and_preserves_final_output(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = self._write_running_state(td)
            terminated = []
            manager = SubagentManager(
                root_dir=td,
                process_exists=lambda pid: pid == 1234,
                terminate_process=lambda pid: terminated.append(pid),
                sleep=lambda _: None,
            )

            result = manager.close_agent("wc_france_history", reason="parent_cleanup", grace_s=0)

            self.assertEqual(result.previous_state.turn_status, "completed")
            self.assertEqual(result.previous_state.process_status, "waiting_reply")
            self.assertEqual(result.closed_state.process_status, "killed")
            self.assertEqual(Path(result.final_output_path), task_dir / "output.txt")
            self.assertEqual(terminated, [1234])
            self.assertTrue((task_dir / "_stop").exists())
            events = (task_dir / "events.jsonl").read_text(encoding="utf-8")
            self.assertIn('"type":"agent_closed"', events)

    def test_close_agent_records_shutdown_when_process_exits_during_grace_period(self):
        with tempfile.TemporaryDirectory() as td:
            self._write_running_state(td)
            checks = {"count": 0}
            terminated = []

            def process_exists(_pid):
                checks["count"] += 1
                return checks["count"] == 1

            manager = SubagentManager(
                root_dir=td,
                process_exists=process_exists,
                terminate_process=lambda pid: terminated.append(pid),
                sleep=lambda _: None,
            )

            result = manager.close_agent("wc_france_history", reason="parent_cleanup", grace_s=1)

            self.assertEqual(result.previous_state.turn_status, "completed")
            self.assertEqual(result.closed_state.process_status, "shutdown")
            self.assertEqual(terminated, [])

    def test_close_agent_appends_sidechain_agent_closed_event_when_session_is_known(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = self._write_running_state(td)
            state_path = task_dir / "state.json"
            state = json.loads(state_path.read_text(encoding="utf-8"))
            state.update({"parent_session_id": "session_close", "run_id": "run_close"})
            atomic_write_json(state_path, state)
            manager = SubagentManager(
                root_dir=td,
                process_exists=lambda pid: False,
                sleep=lambda _: None,
            )

            manager.close_agent("wc_france_history", reason="parent_cleanup", grace_s=0)

            transcript_path = Path(td) / "temp" / "sessions" / "session_close" / "subagents" / "run_close.jsonl"
            rows = [json.loads(line) for line in transcript_path.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows[-1]["type"], "agent_closed")
            self.assertEqual(rows[-1]["payload"]["reason"], "parent_cleanup")

    def test_read_agent_records_worktree_summary_for_worktree_isolation(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = self._write_running_state(td, task_name="writer", pid=None)
            worktree = Path(td) / "temp" / "subagents" / "worktrees" / "run_writer"
            worktree.mkdir(parents=True)
            state_path = task_dir / "state.json"
            raw = json.loads(state_path.read_text(encoding="utf-8"))
            raw.update({"isolation": "worktree", "worktree_path": str(worktree), "process_status": "exited"})
            atomic_write_json(state_path, raw)
            calls = []

            def fake_runner(cmd, **kwargs):
                calls.append((cmd, kwargs))
                if cmd[-1] == "--short":
                    return type("Result", (), {"returncode": 0, "stdout": " M agentmain.py\n?? docs/new.md\n", "stderr": ""})()
                return type("Result", (), {"returncode": 0, "stdout": " agentmain.py | 1 +\n docs/new.md | 2 ++\n", "stderr": ""})()

            manager = SubagentManager(root_dir=td, process_exists=lambda _pid: False, worktree_runner=fake_runner)

            state = manager.read_agent("writer")

            persisted = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(state.worktree_summary["status"], "dirty")
            self.assertEqual(state.worktree_summary["changed_files"], ["agentmain.py", "docs/new.md"])
            self.assertEqual(persisted["worktree_summary"], state.worktree_summary)
            self.assertEqual(calls[0][0], ["git", "-C", str(worktree), "status", "--short"])

    def test_close_agent_can_capture_and_cleanup_worktree(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = self._write_running_state(td, task_name="writer", pid=None)
            worktree = Path(td) / "temp" / "subagents" / "worktrees" / "run_writer"
            worktree.mkdir(parents=True)
            (worktree / "leftover.txt").write_text("leftover", encoding="utf-8")
            state_path = task_dir / "state.json"
            raw = json.loads(state_path.read_text(encoding="utf-8"))
            raw.update({"isolation": "worktree", "worktree_path": str(worktree), "process_status": "exited"})
            atomic_write_json(state_path, raw)
            calls = []

            def fake_runner(cmd, **kwargs):
                calls.append((cmd, kwargs))
                if cmd[-1] == "--short":
                    return type("Result", (), {"returncode": 0, "stdout": " M agentmain.py\n", "stderr": ""})()
                return type("Result", (), {"returncode": 0, "stdout": "diffstat\n", "stderr": ""})()

            manager = SubagentManager(root_dir=td, process_exists=lambda _pid: False, sleep=lambda _: None, worktree_runner=fake_runner)

            result = manager.close_agent("writer", reason="done", grace_s=0, cleanup_worktree=True)

            self.assertFalse(worktree.exists())
            state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(result.closed_state.worktree_summary["changed_files"], ["agentmain.py"])
            self.assertEqual(state["worktree_cleanup"]["status"], "removed")
            self.assertEqual(
                calls[-1][0],
                ["git", "-C", str(Path(td)), "worktree", "remove", "--force", str(worktree)],
            )

    def test_read_agent_preserves_closed_process_status_after_shutdown(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = self._write_running_state(td)
            state_path = task_dir / "state.json"
            raw = json.loads(state_path.read_text(encoding="utf-8"))
            raw["turn_status"] = "completed"
            raw["process_status"] = "shutdown"
            raw["final_output_path"] = raw["output_path"]
            atomic_write_json(state_path, raw)
            manager = SubagentManager(root_dir=td, process_exists=lambda pid: False)

            state = manager.read_agent("wc_france_history")

            self.assertEqual(state.turn_status, "completed")
            self.assertEqual(state.process_status, "shutdown")


class CascadeCloseTest(unittest.TestCase):
    """`close_agent(cascade=true)` — the one P0 item in the v2 design that never landed.

    §6.2 of docs/ga_subagent_v2_optimization_design_2026-07-27.md specifies a `cascade` field
    and a `closed_descendants` return value, and §6.3 lists "close descendants" as a P0 test,
    but the implementation only ever closed the target. Closing a middle-tier agent therefore
    orphaned everything below it: those processes keep running with nobody reading their
    output, and each one keeps consuming a G1 active-agent slot until its process happens to
    die and the stale-row reaper notices. Cascade is what makes close a tree operation.
    """

    def _agent(self, manager, td, parent, name, pid):
        entry = manager.registry.create_child(
            parent,
            name,
            Path(td) / "temp" / name,
            Path(td) / "temp" / name / "state.json",
            pid=pid,
        )
        task_dir = Path(entry.task_dir)
        task_dir.mkdir(parents=True, exist_ok=True)
        output_path = task_dir / "output.txt"
        # No [ROUND END] marker: these agents are mid-turn, which is the only state where a
        # cascade matters. With the marker, read_agent would promote them to completed and the
        # close would be a no-op on an already-finished process.
        output_path.write_text(f"{name} is still working", encoding="utf-8")
        atomic_write_json(
            task_dir / "state.json",
            {
                "schema_version": 1,
                "task_name": name,
                "agent_path": str(entry.agent_path),
                "run_id": entry.run_id,
                "pid": pid,
                "round": 0,
                "turn_status": "running",
                "process_status": "alive",
                "output_path": str(output_path),
                "final_output_path": None,
            },
        )
        return entry

    def _manager(self, td, terminated):
        return SubagentManager(
            root_dir=td,
            process_exists=lambda _pid: True,
            terminate_process=lambda pid: terminated.append(pid),
            sleep=lambda _: None,
        )

    def _tree(self, manager, td):
        """/root/top -> /root/top/mid -> /root/top/mid/leaf, plus an unrelated sibling."""
        manager.registry.max_depth = 0
        top = self._agent(manager, td, "/root", "top", 100)
        mid = self._agent(manager, td, top.agent_path, "mid", 200)
        leaf = self._agent(manager, td, mid.agent_path, "leaf", 300)
        other = self._agent(manager, td, "/root", "other", 400)
        return top, mid, leaf, other

    def test_cascade_closes_descendants_deepest_first_and_leaves_unrelated_agents_alone(self):
        with tempfile.TemporaryDirectory() as td:
            terminated = []
            manager = self._manager(td, terminated)
            top, mid, leaf, other = self._tree(manager, td)

            result = manager.close_agent(str(top.agent_path), reason="parent_cleanup", grace_s=0, cascade=True)

            self.assertEqual(
                [row["agent_path"] for row in result.closed_descendants],
                [str(leaf.agent_path), str(mid.agent_path)],
            )
            # The target is terminated last: its children must be gone before it is.
            self.assertEqual(terminated, [300, 200, 100])
            self.assertEqual(manager.registry.get(other.agent_path).status, "running")
            self.assertEqual([str(e.agent_path) for e in manager.registry.list_agents()], [str(other.agent_path)])

    def test_cascade_defaults_to_false_so_existing_close_behaviour_is_unchanged(self):
        with tempfile.TemporaryDirectory() as td:
            terminated = []
            manager = self._manager(td, terminated)
            top, mid, leaf, _other = self._tree(manager, td)

            result = manager.close_agent(str(top.agent_path), reason="parent_cleanup", grace_s=0)

            self.assertEqual(result.closed_descendants, [])
            self.assertEqual(terminated, [100])
            self.assertEqual(manager.registry.get(mid.agent_path).status, "running")
            self.assertEqual(manager.registry.get(leaf.agent_path).status, "running")

    def test_each_cascaded_descendant_gets_a_real_close_not_just_a_registry_flag(self):
        """A row flipped to closed while the process keeps running is worse than no cascade."""
        with tempfile.TemporaryDirectory() as td:
            terminated = []
            manager = self._manager(td, terminated)
            top, mid, leaf, _other = self._tree(manager, td)

            manager.close_agent(str(top.agent_path), grace_s=0, cascade=True)

            for entry in (mid, leaf):
                task_dir = Path(entry.task_dir)
                state = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
                self.assertEqual(state["turn_status"], "interrupted", entry.task_name)
                self.assertEqual(state["process_status"], "killed", entry.task_name)
                self.assertEqual(state["close_reason"], f"cascade_close:{top.agent_path}", entry.task_name)
                self.assertTrue((task_dir / "_stop").exists(), entry.task_name)
                self.assertIn('"type":"agent_closed"', (task_dir / "events.jsonl").read_text(encoding="utf-8"))

    def test_a_descendant_that_fails_to_close_is_reported_and_does_not_abort_the_cascade(self):
        """Half a cascade with an exception is the worst outcome: the target stays alive too."""
        with tempfile.TemporaryDirectory() as td:
            terminated = []
            manager = self._manager(td, terminated)
            top, mid, leaf, _other = self._tree(manager, td)
            real_close = manager._close_single_agent

            def flaky(target, **kwargs):
                if str(target) == str(mid.agent_path):
                    raise OSError("state.json is locked by another process")
                return real_close(target, **kwargs)

            manager._close_single_agent = flaky

            result = manager.close_agent(str(top.agent_path), grace_s=0, cascade=True)

            rows = {row["agent_path"]: row for row in result.closed_descendants}
            self.assertEqual(rows[str(leaf.agent_path)]["status"], "closed")
            self.assertEqual(rows[str(mid.agent_path)]["status"], "error")
            self.assertIn("locked", rows[str(mid.agent_path)]["msg"])
            self.assertEqual(result.closed_state.process_status, "killed")
            self.assertEqual(manager.registry.get(top.agent_path).status, "closed")

    def test_cascade_on_a_leaf_reports_no_descendants(self):
        with tempfile.TemporaryDirectory() as td:
            terminated = []
            manager = self._manager(td, terminated)
            _top, _mid, leaf, _other = self._tree(manager, td)

            result = manager.close_agent(str(leaf.agent_path), grace_s=0, cascade=True)

            self.assertEqual(result.closed_descendants, [])
            self.assertEqual(terminated, [300])

    def test_cascade_reason_records_which_ancestor_triggered_the_close(self):
        """`parent_cleanup` on a descendant hides why it died; the ancestor path is the reason."""
        with tempfile.TemporaryDirectory() as td:
            terminated = []
            manager = self._manager(td, terminated)
            top, mid, _leaf, _other = self._tree(manager, td)

            result = manager.close_agent(str(top.agent_path), reason="user_abort", grace_s=0, cascade=True)

            rows = {row["agent_path"]: row for row in result.closed_descendants}
            self.assertEqual(rows[str(mid.agent_path)]["reason"], f"cascade_close:{top.agent_path}")
            self.assertEqual(result.closed_state.close_reason, "user_abort")


class SubagentManagerSpawnWaitMailboxTest(unittest.TestCase):
    def test_spawn_agent_duplicate_task_name_uses_new_agent_path_and_preserves_first_artifact(self):
        with tempfile.TemporaryDirectory() as td:
            class FakeProcess:
                next_pid = 3000

                def __init__(self):
                    self.pid = FakeProcess.next_pid
                    FakeProcess.next_pid += 1

            manager = SubagentManager(
                root_dir=td,
                popen=lambda *_, **__: FakeProcess(),
                python_executable="python-test",
                # The first agent must be gone for the rename to be the right answer; a live name
                # is refused instead (SpawnNameConflictTest). Stated explicitly so this test does
                # not silently depend on whether pid 3000 happens to exist on the host.
                process_exists=lambda _pid: False,
            )

            first = manager.spawn_agent("researcher", "first prompt")
            (Path(first.task_dir) / "output.txt").write_text("first answer\n\n[ROUND END]\n", encoding="utf-8")
            second = manager.spawn_agent("researcher", "second prompt")

            self.assertEqual(first.task_name, "researcher")
            self.assertEqual(first.agent_path, "/root/researcher")
            self.assertEqual(second.task_name, "researcher_1")
            self.assertEqual(second.agent_path, "/root/researcher_1")
            self.assertNotEqual(first.task_dir, second.task_dir)
            self.assertNotEqual(first.run_id, second.run_id)
            self.assertTrue((Path(first.task_dir) / "output.txt").exists())
            self.assertEqual((Path(first.task_dir) / "input.txt").read_text(encoding="utf-8"), "first prompt")
            self.assertEqual((Path(second.task_dir) / "input.txt").read_text(encoding="utf-8"), "second prompt")

    def test_list_agents_uses_registry_and_can_include_closed_entries(self):
        with tempfile.TemporaryDirectory() as td:
            manager = SubagentManager(
                root_dir=td,
                popen=lambda *_, **__: type("FakeProcess", (), {"pid": 1})(),
                python_executable="python-test",
                process_exists=lambda _pid: False,
                sleep=lambda _: None,
            )
            manager.spawn_agent("researcher", "prompt")
            manager.close_agent("/root/researcher", grace_s=0)

            self.assertEqual(manager.list_agents(), [])
            closed = manager.list_agents(include_closed=True)
            self.assertEqual([state.agent_path for state in closed], ["/root/researcher"])
            self.assertEqual(closed[0].process_status, "shutdown")

    def test_spawn_agent_records_parent_permission_mode_for_inherit_profile(self):
        with tempfile.TemporaryDirectory() as td:
            manager = SubagentManager(
                root_dir=td,
                popen=lambda *_, **__: type("FakeProcess", (), {"pid": 246})(),
                python_executable="python-test",
            )

            handle = manager.spawn_agent(
                "inherit_worker",
                "inherit current permissions",
                parent_permission_mode="read_only",
            )

            state = json.loads((Path(handle.task_dir) / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["permission_profile"], "inherit-current-permissions")
            self.assertEqual(state["parent_permission_mode"], "read_only")
            self.assertIn("--parent_permission_mode", handle.command)
            self.assertIn("read_only", handle.command)
            registry = json.loads((Path(td) / "temp" / "subagents" / "registry.json").read_text(encoding="utf-8"))
            entry = registry["agents"]["/root/inherit_worker"]
            self.assertEqual(entry["parent_permission_mode"], "read_only")

    def test_spawn_agent_records_permission_metadata_in_state_registry_and_events(self):
        with tempfile.TemporaryDirectory() as td:
            manager = SubagentManager(
                root_dir=td,
                popen=lambda *_, **__: type("FakeProcess", (), {"pid": 1357})(),
                python_executable="python-test",
            )

            handle = manager.spawn_agent(
                "readonly_worker",
                "inspect only",
                permission_profile="read_only",
                permission_options={"denied_tools": ["code_run"]},
            )

            task_dir = Path(handle.task_dir)
            state = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["permission_profile"], "read_only")
            self.assertEqual(state["permission_options"], {"denied_tools": ["code_run"]})
            registry = json.loads((Path(td) / "temp" / "subagents" / "registry.json").read_text(encoding="utf-8"))
            entry = registry["agents"]["/root/readonly_worker"]
            self.assertEqual(entry["permission_profile"], "read_only")
            self.assertEqual(entry["permission_options"], {"denied_tools": ["code_run"]})
            cmd = handle.command
            self.assertIn("--permission_profile", cmd)
            self.assertIn("read_only", cmd)
            self.assertIn("--permission_options", cmd)
            event_rows = [
                json.loads(line)
                for line in (task_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(event_rows[-1]["permission_profile"], "read_only")

    def test_spawn_agent_worktree_isolation_records_created_worktree_and_cwd(self):
        with tempfile.TemporaryDirectory() as td:
            repo_git = Path(td) / ".git"
            repo_git.mkdir()
            calls = {"popen": [], "worktree": []}

            def fake_popen(cmd, **kwargs):
                calls["popen"].append((cmd, kwargs))
                return type("FakeProcess", (), {"pid": 9876})()

            def fake_worktree(repo_dir, base_dir, run_id):
                calls["worktree"].append((repo_dir, base_dir, run_id))
                path = Path(td) / "temp" / "subagents" / "worktrees" / run_id
                path.mkdir(parents=True)
                return {"status": "created", "path": str(path), "run_id": run_id}

            manager = SubagentManager(
                root_dir=td,
                popen=fake_popen,
                python_executable="python-test",
                worktree_creator=fake_worktree,
            )

            handle = manager.spawn_agent("writer", "edit files", isolation="worktree")

            self.assertEqual(handle.isolation, "worktree")
            self.assertTrue(handle.worktree_path.endswith(handle.run_id))
            self.assertEqual(calls["worktree"][0][0], Path(td))
            cmd, kwargs = calls["popen"][0]
            self.assertEqual(Path(cmd[1]), Path(handle.worktree_path) / "agentmain.py")
            self.assertEqual(Path(kwargs["cwd"]), Path(handle.worktree_path))
            self.assertIn("--task_root", cmd)
            self.assertIn(str(Path(td)), cmd)
            state = json.loads((Path(handle.task_dir) / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["isolation"], "worktree")
            self.assertEqual(state["worktree_path"], handle.worktree_path)

    def test_spawn_agent_marks_registry_closed_when_worktree_creation_fails(self):
        with tempfile.TemporaryDirectory() as td:
            repo_git = Path(td) / ".git"
            repo_git.mkdir()

            def failing_worktree(_repo_dir, _base_dir, _run_id):
                raise RuntimeError("worktree failed")

            manager = SubagentManager(
                root_dir=td,
                popen=lambda *_, **__: type("FakeProcess", (), {"pid": 1})(),
                python_executable="python-test",
                worktree_creator=failing_worktree,
            )

            with self.assertRaises(RuntimeError):
                manager.spawn_agent("writer", "edit files", isolation="worktree")

            registry = json.loads((Path(td) / "temp" / "subagents" / "registry.json").read_text(encoding="utf-8"))
            entry = registry["agents"]["/root/writer"]
            self.assertEqual(entry["status"], "closed")
            self.assertEqual(entry["closed_status"], "worktree_error")
            state = json.loads((Path(td) / "temp" / "writer" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["turn_status"], "errored")
            self.assertEqual(state["process_status"], "exited")
            self.assertIn("worktree failed", state["last_error"])

    def test_spawn_agent_records_role_metadata_and_background_defaults(self):
        with tempfile.TemporaryDirectory() as td:
            manager = SubagentManager(
                root_dir=td,
                popen=lambda *_, **__: type("FakeProcess", (), {"pid": 4321})(),
                python_executable="python-test",
            )

            handle = manager.spawn_agent(
                "researcher",
                "role wrapped prompt",
                agent_type="researcher",
                role_source_path=str(Path(td) / ".ga" / "subagents" / "researcher.json"),
                background=False,
                ipc_mode="socket",
            )

            self.assertEqual(handle.agent_type, "researcher")
            self.assertFalse(handle.background)
            self.assertEqual(handle.ipc_mode, "socket")
            self.assertEqual(handle.effective_ipc_mode, "file")
            state = json.loads((Path(handle.task_dir) / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["agent_type"], "researcher")
            self.assertTrue(state["role_source_path"].endswith("researcher.json"))
            self.assertFalse(state["background"])
            self.assertEqual(state["ipc_mode"], "socket")
            self.assertEqual(state["effective_ipc_mode"], "file")
            self.assertIn("durable file event bus", state["ipc_fallback_reason"])
            registry = json.loads((Path(td) / "temp" / "subagents" / "registry.json").read_text(encoding="utf-8"))
            entry = registry["agents"]["/root/researcher"]
            self.assertEqual(entry["agent_type"], "researcher")
            self.assertFalse(entry["background"])
            self.assertEqual(entry["ipc_mode"], "socket")
            self.assertEqual(entry["effective_ipc_mode"], "file")

    def test_spawn_agent_enables_realtime_ipc_channel_and_publishes_bus_events(self):
        with tempfile.TemporaryDirectory() as td:
            created = []

            class FakeChannel:
                def __init__(self, run_id):
                    self.run_id = run_id
                    self.address = rf"\\.\pipe\ga_subagent_{run_id}"
                    self.published = []
                    self.closed = False

                def start(self):
                    return self

                def endpoint(self):
                    return {"status": "listening", "address": self.address, "family": "AF_PIPE", "subscriber_count": 0}

                def publish(self, event):
                    self.published.append(event)
                    return 1

                def close(self):
                    self.closed = True

            def factory(run_id, task_name):
                channel = FakeChannel(run_id)
                created.append((run_id, task_name, channel))
                return channel

            manager = SubagentManager(
                root_dir=td,
                popen=lambda *_, **__: type("FakeProcess", (), {"pid": 7788})(),
                python_executable="python-test",
                process_exists=lambda _pid: False,
                sleep=lambda _: None,
                realtime_channel_factory=factory,
            )

            handle = manager.spawn_agent("realtime_worker", "watch me live", ipc_mode="socket")

            self.assertEqual(handle.ipc_mode, "socket")
            self.assertEqual(handle.effective_ipc_mode, "socket")
            state = json.loads((Path(handle.task_dir) / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["effective_ipc_mode"], "socket")
            self.assertIsNone(state["ipc_fallback_reason"])
            self.assertEqual(state["ipc_endpoint"]["status"], "listening")
            self.assertNotIn("channel", state)
            self.assertEqual(len(created), 1)
            run_id, task_name, channel = created[0]
            self.assertEqual(task_name, "realtime_worker")
            self.assertEqual(run_id, state["run_id"])
            self.assertIn("agent_started", [event["type"] for event in channel.published])
            durable_types = [event["type"] for event in manager.event_bus.read_events_since(0)]
            self.assertIn("agent_started", durable_types)

            manager.close_agent("realtime_worker", reason="test_done", grace_s=0)

            self.assertTrue(channel.closed)
            self.assertIn("agent_closed", [event["type"] for event in channel.published])

    def test_spawn_agent_writes_input_state_and_registry_without_putting_prompt_on_command_line(self):
        with tempfile.TemporaryDirectory() as td:
            calls = []

            class FakeProcess:
                pid = 2468

            def fake_popen(cmd, **kwargs):
                calls.append((cmd, kwargs))
                return FakeProcess()

            manager = SubagentManager(root_dir=td, popen=fake_popen, python_executable="python-test")

            handle = manager.spawn_agent(
                "research_france",
                "long task prompt",
                llm_no=2,
                verbose=True,
                parent_session_id="session_demo",
            )

            task_dir = Path(td) / "temp" / "research_france"
            self.assertEqual(handle.task_name, "research_france")
            self.assertEqual(handle.pid, 2468)
            self.assertEqual((task_dir / "input.txt").read_text(encoding="utf-8"), "long task prompt")
            cmd, kwargs = calls[0]
            self.assertEqual(cmd[0], "python-test")
            self.assertIn("--task", cmd)
            self.assertIn("research_france", cmd)
            self.assertIn("--nobg", cmd)
            self.assertIn("--llm_no", cmd)
            self.assertIn("2", cmd)
            self.assertIn("--verbose", cmd)
            self.assertNotIn("long task prompt", cmd)
            self.assertEqual(Path(kwargs["cwd"]), Path(td))
            state = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["turn_status"], "pending")
            self.assertEqual(state["process_status"], "alive")
            self.assertEqual(state["llm_no"], 2)
            self.assertTrue(state["verbose"])
            registry = json.loads((Path(td) / "temp" / "subagents" / "registry.json").read_text(encoding="utf-8"))
            self.assertEqual(registry["agents"]["/root/research_france"]["pid"], 2468)
            self.assertEqual(
                registry["agents"]["/root/research_france"]["last_task_message"],
                "long task prompt",
            )
            event_rows = [
                json.loads(line)
                for line in (task_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            inbox_rows = [
                json.loads(line)
                for line in (Path(td) / "temp" / "subagents" / "inbox.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(event_rows[-1]["type"], "agent_started")
            self.assertEqual(event_rows[-1]["pid"], 2468)
            self.assertEqual(inbox_rows[-1]["type"], "agent_started")
            self.assertEqual(inbox_rows[-1]["pid"], 2468)

    def test_resume_agent_restarts_same_task_with_transcript_resume_context(self):
        with tempfile.TemporaryDirectory() as td:
            calls = []

            class FakeProcess:
                pid = 4321

            def fake_popen(cmd, **kwargs):
                calls.append((cmd, kwargs))
                return FakeProcess()

            task_dir = Path(td) / "temp" / "resume_child"
            task_dir.mkdir(parents=True)
            output_path = task_dir / "output.txt"
            output_path.write_text("old answer\n\n[ROUND END]\n", encoding="utf-8")
            (task_dir / "_stop").write_text("old stop", encoding="utf-8")
            (task_dir / "reply.txt").write_text("old reply", encoding="utf-8")
            from subagent_artifacts import SubagentArtifactStore

            run_dir = Path(td) / "temp" / "subagents" / "runs" / "run_resume_child"
            old_artifact = SubagentArtifactStore(run_dir).record_final_output(output_path, round_no=0)
            atomic_write_json(
                task_dir / "state.json",
                {
                    "schema_version": 1,
                    "task_name": "resume_child",
                    "agent_path": "/root/resume_child",
                    "parent_session_id": "session_resume",
                    "run_id": "run_resume_child",
                    "artifact_dir": str(Path(td) / "temp" / "subagents" / "runs" / "run_resume_child"),
                    "pid": None,
                    "round": 0,
                    "turn_status": "completed",
                    "process_status": "shutdown",
                    "output_path": str(output_path),
                    "final_output_path": str(output_path),
                    "final_output_ref": old_artifact["artifact_id"],
                    "permission_profile": "read_only",
                    "parent_permission_mode": "read_only",
                    "permission_options": {},
                    "llm_no": 2,
                    "verbose": True,
                    "background": True,
                    "ipc_mode": "file",
                    "effective_ipc_mode": "file",
                    "ipc_fallback_reason": None,
                },
            )
            from subagent_transcript import SubagentTranscriptStore

            store = SubagentTranscriptStore(Path(td) / "temp" / "sessions")
            store.write_metadata(session_id="session_resume", run_id="run_resume_child", agent_path="/root/resume_child")
            store.append_event("session_resume", "run_resume_child", "request", {"prompt": "original task"})
            store.append_event("session_resume", "run_resume_child", "assistant", {"content": "old analysis"})
            store.append_event("session_resume", "run_resume_child", "final_output", {"final_output": "old answer"})
            manager = SubagentManager(root_dir=td, popen=fake_popen, python_executable="python-test")
            entry = manager.registry.create_child(
                parent_path="/root",
                task_name="resume_child",
                task_dir=task_dir,
                state_path=task_dir / "state.json",
                parent_session_id="session_resume",
                permission_profile="read_only",
                parent_permission_mode="read_only",
            )
            manager.registry.update(
                entry.agent_path,
                run_id="run_resume_child",
                artifact_dir=str(Path(td) / "temp" / "subagents" / "runs" / "run_resume_child"),
            )
            manager.registry.mark_closed(entry.agent_path, previous_status="completed", closed_status="shutdown")

            result = manager.resume_agent("resume_child", "continue from transcript")

            self.assertEqual(result.previous_state.agent_path, "/root/resume_child")
            self.assertEqual(result.handle.task_name, "resume_child")
            self.assertEqual(result.handle.run_id, "run_resume_child")
            self.assertEqual((task_dir / "input.txt").read_text(encoding="utf-8"), "continue from transcript")
            self.assertFalse((task_dir / "_stop").exists())
            self.assertFalse((task_dir / "reply.txt").exists())
            history = json.loads((task_dir / "_history.json").read_text(encoding="utf-8"))
            self.assertEqual(history[0]["role"], "user")
            self.assertIn("original task", history[0]["content"])
            self.assertIn("old answer", history[-1]["content"])
            state = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["pid"], 4321)
            self.assertEqual(state["round"], 1)
            self.assertEqual(state["turn_status"], "pending")
            self.assertEqual(state["process_status"], "alive")
            self.assertIsNone(state.get("final_output_ref"))
            self.assertEqual(state["llm_no"], 2)
            self.assertTrue(state["verbose"])
            self.assertEqual(Path(state["output_path"]), task_dir / "output1.txt")
            self.assertEqual(output_path.read_text(encoding="utf-8"), "old answer\n\n[ROUND END]\n")
            self.assertEqual(state["last_message"], "continue from transcript")
            self.assertEqual(state["resume_source"], "sidechain_transcript")
            registry = json.loads((Path(td) / "temp" / "subagents" / "registry.json").read_text(encoding="utf-8"))
            self.assertEqual(registry["agents"]["/root/resume_child"]["run_id"], "run_resume_child")
            self.assertEqual(registry["agents"]["/root/resume_child"]["status"], "running")
            self.assertEqual(calls[0][0][0], "python-test")
            self.assertIn("--task", calls[0][0])
            self.assertIn("resume_child", calls[0][0])
            self.assertIn("--llm_no", calls[0][0])
            self.assertIn("2", calls[0][0])
            self.assertIn("--verbose", calls[0][0])
            events = [json.loads(line) for line in (task_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
            self.assertEqual(events[-1]["type"], "agent_resumed")
            self.assertEqual(events[-1]["run_id"], "run_resume_child")

    def test_spawn_agent_reports_agent_error_when_process_launch_fails(self):
        with tempfile.TemporaryDirectory() as td:
            def fake_popen(_cmd, **_kwargs):
                raise OSError("cannot launch child")

            manager = SubagentManager(root_dir=td, popen=fake_popen, python_executable="python-test")

            with self.assertRaises(OSError):
                manager.spawn_agent("broken_child", "prompt")

            task_dir = Path(td) / "temp" / "broken_child"
            state = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["turn_status"], "errored")
            self.assertEqual(state["process_status"], "exited")
            self.assertIn("cannot launch child", state["last_error"])
            event_rows = [
                json.loads(line)
                for line in (task_dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            inbox_rows = [
                json.loads(line)
                for line in (Path(td) / "temp" / "subagents" / "inbox.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([row["type"] for row in event_rows], ["agent_error"])
            self.assertEqual([row["type"] for row in inbox_rows], ["agent_error"])
            registry = json.loads((Path(td) / "temp" / "subagents" / "registry.json").read_text(encoding="utf-8"))
            self.assertEqual(registry["agents"]["/root/broken_child"]["pid"], None)

    def test_spawn_agent_rejects_unsafe_task_names(self):
        with tempfile.TemporaryDirectory() as td:
            manager = SubagentManager(root_dir=td, popen=lambda *_, **__: None)

            for bad_name in ["../escape", "UpperCase", "has-dash", "space name", ""]:
                with self.subTest(bad_name=bad_name):
                    with self.assertRaises(ValueError):
                        manager.spawn_agent(bad_name, "prompt")

    def test_spawn_agent_records_fork_history_count_estimate_and_warning(self):
        with tempfile.TemporaryDirectory() as td:
            manager = SubagentManager(
                root_dir=td,
                popen=lambda *_, **__: type("FakeProcess", (), {"pid": 1})(),
                python_executable="python-test",
            )
            history = [
                {"role": "user", "content": "one"},
                {"role": "assistant", "content": "two"},
            ]

            manager.spawn_agent("fork_meta", "prompt", fork_turns="all", fork_history=history)

            state = json.loads((Path(td) / "temp" / "fork_meta" / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["fork_history_count"], 2)
            self.assertGreater(state["fork_history_token_estimate"], 0)
            self.assertEqual(state["fork_redacted"], False)
            self.assertIn("full history fork", state["fork_policy_warning"])

    def test_spawn_agent_fork_turns_all_writes_full_history(self):
        with tempfile.TemporaryDirectory() as td:
            manager = SubagentManager(
                root_dir=td,
                popen=lambda *_, **__: type("FakeProcess", (), {"pid": 1})(),
                python_executable="python-test",
            )
            history = [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "second"},
            ]

            manager.spawn_agent("fork_all", "prompt", fork_turns="all", fork_history=history)

            task_dir = Path(td) / "temp" / "fork_all"
            self.assertEqual(
                json.loads((task_dir / "_history.json").read_text(encoding="utf-8")),
                history,
            )
            state = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["fork_turns"], "all")

    def test_spawn_agent_fork_turns_last_n_writes_recent_history(self):
        with tempfile.TemporaryDirectory() as td:
            manager = SubagentManager(
                root_dir=td,
                popen=lambda *_, **__: type("FakeProcess", (), {"pid": 1})(),
                python_executable="python-test",
            )
            history = [
                {"role": "user", "content": "one"},
                {"role": "assistant", "content": "two"},
                {"role": "user", "content": "three"},
            ]

            manager.spawn_agent("fork_recent", "prompt", fork_turns="2", fork_history=history)

            task_dir = Path(td) / "temp" / "fork_recent"
            self.assertEqual(
                json.loads((task_dir / "_history.json").read_text(encoding="utf-8")),
                history[-2:],
            )
            state = json.loads((task_dir / "state.json").read_text(encoding="utf-8"))
            self.assertEqual(state["fork_turns"], "2")

    def test_spawn_agent_fork_turns_none_does_not_write_history(self):
        with tempfile.TemporaryDirectory() as td:
            manager = SubagentManager(
                root_dir=td,
                popen=lambda *_, **__: type("FakeProcess", (), {"pid": 1})(),
                python_executable="python-test",
            )

            manager.spawn_agent(
                "fork_none",
                "prompt",
                fork_turns="none",
                fork_history=[{"role": "user", "content": "ignored"}],
            )

            self.assertFalse((Path(td) / "temp" / "fork_none" / "_history.json").exists())

    def test_spawn_agent_rejects_invalid_fork_turns(self):
        with tempfile.TemporaryDirectory() as td:
            manager = SubagentManager(root_dir=td, popen=lambda *_, **__: None)

            for bad_value in ["", "0", "-1", "abc"]:
                with self.subTest(bad_value=bad_value):
                    with self.assertRaises(ValueError):
                        manager.spawn_agent("fork_bad", "prompt", fork_turns=bad_value, fork_history=[])

    def test_spawn_agent_rejects_missing_history_when_fork_requested(self):
        with tempfile.TemporaryDirectory() as td:
            manager = SubagentManager(root_dir=td, popen=lambda *_, **__: None)

            with self.assertRaises(ValueError):
                manager.spawn_agent("fork_missing", "prompt", fork_turns="all")

    def test_wait_agents_returns_when_events_file_changes(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td) / "temp" / "demo"
            task_dir.mkdir(parents=True)
            atomic_write_json(
                task_dir / "state.json",
                {
                    "schema_version": 1,
                    "task_name": "demo",
                    "agent_path": "/root/demo",
                    "pid": 1,
                    "round": 0,
                    "turn_status": "running",
                    "process_status": "alive",
                },
            )
            manager = SubagentManager(root_dir=td, process_exists=lambda pid: True)

            def append_later():
                time.sleep(0.05)
                (task_dir / "events.jsonl").write_text('{"type":"turn_completed"}\n', encoding="utf-8")

            thread = threading.Thread(target=append_later)
            thread.start()
            result = manager.wait_agents(["demo"], timeout_s=1, poll_interval_s=0.01)
            thread.join(timeout=1)

            self.assertFalse(result.timed_out)
            self.assertEqual([state.task_name for state in result.changed_agents], ["demo"])

    def test_wait_agents_returns_when_parent_inbox_receives_update(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td) / "temp" / "demo"
            task_dir.mkdir(parents=True)
            atomic_write_json(
                task_dir / "state.json",
                {
                    "schema_version": 1,
                    "task_name": "demo",
                    "agent_path": "/root/demo",
                    "pid": 1,
                    "round": 0,
                    "turn_status": "running",
                    "process_status": "alive",
                },
            )
            manager = SubagentManager(root_dir=td, process_exists=lambda pid: True)

            def notify_later():
                time.sleep(0.05)
                append_jsonl_event(
                    Path(td) / "temp" / "subagents" / "inbox.jsonl",
                    {"type": "turn_completed", "task_name": "demo", "agent_path": "/root/demo"},
                )

            thread = threading.Thread(target=notify_later)
            thread.start()
            result = manager.wait_agents(["demo"], timeout_s=1, poll_interval_s=0.01)
            thread.join(timeout=1)

            self.assertFalse(result.timed_out)
            self.assertEqual([state.task_name for state in result.changed_agents], ["demo"])
            self.assertEqual(result.message, "Subagent mailbox update received.")
            self.assertEqual(result.events[0]["type"], "turn_completed")

    def test_list_agents_ignores_non_agent_temp_dirs(self):
        with tempfile.TemporaryDirectory() as td:
            temp_root = Path(td) / "temp"
            temp_root.mkdir(parents=True)
            for name in [".playwright-cli", "ga-real-e2e-tmp", "model_responses", "sessions"]:
                (temp_root / name).mkdir()
            (temp_root / "ga-real-e2e-tmp" / "state.json").write_text("{}", encoding="utf-8")
            state_less_dir = temp_root / "valid_no_state"
            state_less_dir.mkdir()
            task_dir = temp_root / "demo_agent"
            task_dir.mkdir()
            atomic_write_json(
                task_dir / "state.json",
                {
                    "schema_version": 1,
                    "task_name": "demo_agent",
                    "agent_path": "/root/demo_agent",
                    "pid": 1,
                    "round": 0,
                    "turn_status": "running",
                    "process_status": "alive",
                },
            )
            manager = SubagentManager(root_dir=td, process_exists=lambda pid: True)

            states = manager.list_agents()
            result = manager.wait_agents([], timeout_s=1, poll_interval_s=0.01)

            self.assertEqual([state.task_name for state in states], ["demo_agent"])
            self.assertFalse((state_less_dir / "state.json").exists())
            self.assertTrue(result.timed_out)
            self.assertEqual(result.changed_agents, [])
            self.assertEqual(result.message, "No subagents to wait for.")

    def test_send_message_queues_without_reply_and_followup_task_triggers_mailbox_turn(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = Path(td) / "temp" / "demo"
            task_dir.mkdir(parents=True)
            manager = SubagentManager(root_dir=td)

            manager.send_message("demo", "queued only", author="/root")
            self.assertFalse((task_dir / "reply.txt").exists())
            manager.followup_task("demo", "run next", author="/root")

            mailbox_rows = [
                json.loads(line)
                for line in (task_dir / "mailbox.jsonl").read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual([row["content"] for row in mailbox_rows], ["queued only", "run next"])
            self.assertEqual([row["delivery_mode"] for row in mailbox_rows], ["queue_only", "trigger_turn"])
            self.assertEqual([row["trigger_turn"] for row in mailbox_rows], [False, True])
            self.assertTrue(all(row["author"] == "/root" for row in mailbox_rows))
            self.assertFalse((task_dir / "reply.txt").exists())


class WaitAgentsWriteAmplificationTest(unittest.TestCase):
    """B3: every wait poll called read_agent(), which writes state.json *and* registry.json.

    Measured (docs/ga_subagent_control_plane_defects_2026-07-30.md §3): 4 agents, a 2s wait at
    0.5s intervals produced 40 atomic writes — 20 to state.json, 20 to registry.json, i.e. 20
    writes/sec of tmp-file + os.replace for a loop that is supposed to be *observing*. Those
    registry writes were also the main feeder of the M5 race. Waiting must not write.
    """

    def _running_agent(self, td, name, pid):
        task_dir = Path(td) / "temp" / name
        task_dir.mkdir(parents=True)
        # No [ROUND END]: a mid-turn agent is the only case where a wait loop actually spins.
        (task_dir / "output.txt").write_text(f"{name} working", encoding="utf-8")
        atomic_write_json(
            task_dir / "state.json",
            {
                "schema_version": 1,
                "task_name": name,
                "agent_path": f"/root/{name}",
                "pid": pid,
                "round": 0,
                "turn_status": "running",
                "process_status": "alive",
            },
        )
        return task_dir

    def _count_writes(self):
        """Count atomic_write_json across every module the wait loop can reach."""
        import subagent_manager
        import subagent_registry
        import subagent_state

        counts = []
        patches = []
        for module in (subagent_state, subagent_manager, subagent_registry):
            real = module.atomic_write_json

            def counting(path, data, _real=real):
                counts.append(Path(path).name)
                return _real(path, data)

            patches.append((module, real, counting))
            module.atomic_write_json = counting

        def restore():
            for module, real, _counting in patches:
                module.atomic_write_json = real

        self.addCleanup(restore)
        return counts

    def test_a_timing_out_wait_writes_nothing(self):
        with tempfile.TemporaryDirectory() as td:
            for index in range(4):
                self._running_agent(td, f"w{index}", 100 + index)
            manager = SubagentManager(root_dir=td, process_exists=lambda _pid: True, sleep=lambda _: None)
            writes = self._count_writes()

            result = manager.wait_agents(["w0", "w1", "w2", "w3"], timeout_s=0.3, poll_interval_s=0.01)

            self.assertTrue(result.timed_out)
            self.assertEqual(writes, [], f"wait wrote {len(writes)} files: {sorted(set(writes))}")

    def test_wait_still_reports_a_finished_agent_with_its_full_state(self):
        """The cheap probe is only for detection; the returned state must stay the real one."""
        with tempfile.TemporaryDirectory() as td:
            task_dir = self._running_agent(td, "done", 200)
            (task_dir / "output.txt").write_text("answer\n\n[ROUND END]\n", encoding="utf-8")
            manager = SubagentManager(root_dir=td, process_exists=lambda _pid: True, sleep=lambda _: None)

            result = manager.wait_agents(["done"], timeout_s=1, poll_interval_s=0.01)

            self.assertFalse(result.timed_out)
            self.assertEqual([state.task_name for state in result.changed_agents], ["done"])
            state = result.changed_agents[0]
            self.assertEqual(state.turn_status, "completed")
            self.assertEqual(state.final_output_path, str(task_dir / "output.txt"))
            persisted = read_json_or_none(task_dir / "state.json")
            self.assertEqual(persisted.get("turn_status"), "completed", "a state worth returning is worth persisting")
            self.assertTrue(persisted.get("final_output_sha256"))

    def test_read_agent_still_persists_when_called_directly(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = self._running_agent(td, "direct", 300)
            manager = SubagentManager(root_dir=td, process_exists=lambda _pid: False, sleep=lambda _: None)

            manager.read_agent("direct")
            # Twice on purpose: the first call creates the registry row (create_child carries no
            # status fields), the second is the update path that must still land.
            manager.read_agent("direct")

            self.assertEqual(read_json_or_none(task_dir / "state.json").get("process_status"), "exited")
            self.assertEqual(manager.registry.get("/root/direct").process_status, "exited")

    def test_probe_agent_reports_the_same_status_as_read_agent_without_writing(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = self._running_agent(td, "probe", 400)
            manager = SubagentManager(root_dir=td, process_exists=lambda _pid: False, sleep=lambda _: None)
            before = (task_dir / "state.json").read_text(encoding="utf-8")
            writes = self._count_writes()

            probed = manager.probe_agent("probe")

            self.assertEqual(writes, [])
            self.assertEqual((task_dir / "state.json").read_text(encoding="utf-8"), before)
            self.assertEqual((probed.turn_status, probed.process_status), ("running", "exited"))


class WaitAgentsWatchTest(unittest.TestCase):
    """B3 watch half: a wait holding live channels must sleep on them, not on a timer.

    Codex `control.rs` `subscribe_status` hands out a `watch::Receiver` for exactly this. The
    fallback matters as much as the wake: with no channel (ipc_mode=file, or realtime refused)
    the loop must keep its blind sleep, or the file transport stops working.
    """

    def _agent(self, td, name, pid=500):
        task_dir = Path(td) / "temp" / name
        task_dir.mkdir(parents=True)
        (task_dir / "output.txt").write_text(f"{name} working", encoding="utf-8")
        atomic_write_json(
            task_dir / "state.json",
            {
                "schema_version": 1,
                "task_name": name,
                "agent_path": f"/root/{name}",
                "pid": pid,
                "round": 0,
                "turn_status": "running",
                "process_status": "alive",
            },
        )
        return task_dir

    def test_wait_blind_sleeps_when_the_agent_has_no_realtime_channel(self):
        with tempfile.TemporaryDirectory() as td:
            self._agent(td, "filed")
            slept = []
            manager = SubagentManager(root_dir=td, process_exists=lambda _pid: True, sleep=slept.append)

            result = manager.wait_agents(["filed"], timeout_s=0.05, poll_interval_s=0.02)

            self.assertTrue(result.timed_out)
            self.assertTrue(slept, "the file transport lost its poll loop")

    def test_wait_sleeps_on_the_channel_when_one_exists(self):
        with tempfile.TemporaryDirectory() as td:
            self._agent(td, "piped")
            slept = []
            waited = []

            class FakeChannel:
                def wait_for_signal(self, timeout):
                    waited.append(timeout)
                    return False

            manager = SubagentManager(root_dir=td, process_exists=lambda _pid: True, sleep=slept.append)
            manager._realtime_channels["/root/piped"] = FakeChannel()

            result = manager.wait_agents(["piped"], timeout_s=0.05, poll_interval_s=0.02)

            self.assertTrue(result.timed_out)
            self.assertTrue(waited, "wait ignored the live channel and fell back to sleeping")
            self.assertEqual(slept, [], "wait both watched the channel and blind-slept")

    def test_a_signal_ends_the_wait_early_so_the_change_is_seen_at_once(self):
        with tempfile.TemporaryDirectory() as td:
            task_dir = self._agent(td, "woken")
            observed = []

            class SignallingChannel:
                def wait_for_signal(self, timeout):
                    observed.append(timeout)
                    (task_dir / "events.jsonl").write_text('{"type":"turn_completed"}\n', encoding="utf-8")
                    return True

            manager = SubagentManager(
                root_dir=td,
                process_exists=lambda _pid: True,
                sleep=lambda _: self.fail("a woken wait must not also sleep"),
            )
            manager._realtime_channels["/root/woken"] = SignallingChannel()

            result = manager.wait_agents(["woken"], timeout_s=5, poll_interval_s=2)

            self.assertFalse(result.timed_out)
            self.assertEqual([state.task_name for state in result.changed_agents], ["woken"])
            self.assertEqual(len(observed), 1, "the wait should need exactly one wake, not a poll cycle")

    def test_the_channel_wait_never_overruns_the_deadline(self):
        with tempfile.TemporaryDirectory() as td:
            self._agent(td, "bounded")
            waited = []

            class FakeChannel:
                def wait_for_signal(self, timeout):
                    waited.append(timeout)
                    return False

            manager = SubagentManager(root_dir=td, process_exists=lambda _pid: True, sleep=lambda _: None)
            manager._realtime_channels["/root/bounded"] = FakeChannel()

            manager.wait_agents(["bounded"], timeout_s=0.05, poll_interval_s=30)

            self.assertTrue(waited)
            self.assertTrue(
                all(value <= 0.05 + 1e-6 for value in waited),
                f"wait would have blocked past its own deadline: {waited}",
            )

    def test_a_broken_channel_falls_back_to_sleeping_instead_of_failing_the_wait(self):
        with tempfile.TemporaryDirectory() as td:
            self._agent(td, "broken")
            slept = []

            class BrokenChannel:
                def wait_for_signal(self, timeout):
                    raise OSError("channel is gone")

            manager = SubagentManager(root_dir=td, process_exists=lambda _pid: True, sleep=slept.append)
            manager._realtime_channels["/root/broken"] = BrokenChannel()

            result = manager.wait_agents(["broken"], timeout_s=0.05, poll_interval_s=0.02)

            self.assertTrue(result.timed_out)
            self.assertTrue(slept, "a dead channel must degrade to polling, not break the wait")


class SubmissionIdempotencyTest(unittest.TestCase):
    """B2: without a submission id, a replayed control-plane call executes twice.

    Measured: two followup_task calls for the same logical submission queued two trigger_turn
    rows — the child ran the task twice. The mailbox already had a message_id dedup branch, but
    nothing above it ever passed an id. Codex attaches `Submission { id, op, trace }` to every op.
    """

    def _manager(self, td):
        class FakeProcess:
            next_pid = 8000

            def __init__(self):
                self.pid = FakeProcess.next_pid
                FakeProcess.next_pid += 1

        return SubagentManager(
            root_dir=td,
            popen=lambda *_, **__: FakeProcess(),
            python_executable="python-test",
            sleep=lambda _: None,
        )

    def _mailbox_rows(self, td, name="worker"):
        from subagent_mailbox import SubagentMailbox

        return SubagentMailbox(Path(td) / "temp" / name / "mailbox.jsonl")._read_rows()

    def test_replayed_followup_task_queues_the_task_once(self):
        with tempfile.TemporaryDirectory() as td:
            manager = self._manager(td)
            manager.spawn_agent("worker", "initial")

            first = manager.followup_task("worker", "do the thing", author="/root", submission_id="sub_a")
            second = manager.followup_task("worker", "do the thing", author="/root", submission_id="sub_a")

            triggers = [row for row in self._mailbox_rows(td) if row.get("trigger_turn")]
            self.assertEqual(len(triggers), 1, "a replayed submission ran the task twice")
            self.assertEqual(first["message_id"], second["message_id"])

    def test_without_a_submission_id_two_calls_stay_two_messages(self):
        """Two genuinely different asks must not be collapsed just because the text matches."""
        with tempfile.TemporaryDirectory() as td:
            manager = self._manager(td)
            manager.spawn_agent("worker", "initial")

            manager.followup_task("worker", "same text", author="/root")
            manager.followup_task("worker", "same text", author="/root")

            self.assertEqual(len([row for row in self._mailbox_rows(td) if row.get("trigger_turn")]), 2)

    def test_replayed_spawn_agent_does_not_start_a_second_process(self):
        with tempfile.TemporaryDirectory() as td:
            spawned = []

            class FakeProcess:
                def __init__(self):
                    self.pid = 8100 + len(spawned)

            def popen(*_a, **_kw):
                spawned.append(1)
                return FakeProcess()

            manager = SubagentManager(root_dir=td, popen=popen, python_executable="python-test", sleep=lambda _: None)

            first = manager.spawn_agent("dup", "same task", submission_id="sub_spawn")
            second = manager.spawn_agent("dup", "same task", submission_id="sub_spawn")

            self.assertEqual(len(spawned), 1, "a replayed spawn started a second real process")
            self.assertEqual(first.agent_path, second.agent_path)
            self.assertEqual(first.run_id, second.run_id)
            self.assertEqual(len(manager.list_agents()), 1)

    def test_the_same_submission_id_on_a_different_agent_still_spawns_it(self):
        """Measured on the real guard E2E: a row from 2026-07-30 suppressed a 2026-08-03 run.

        `temp/subagents/submissions.jsonl` outlives the process (MAX_ROWS 2000, no session
        scoping), and the dedup only compared `(submission_id, op)`. So a fixed id — which is
        exactly what a retrying model produces, and what the schema tells it to send — silently
        returned the *other* agent's handle: no process started, and the caller got a run_id
        belonging to an agent it never asked for. Suppressing an op is bad; answering with the
        wrong agent is worse. The mailbox path already scopes by recipient
        (`_submission_message_id` appends the leaf), so followup/send never had this hole.
        """
        with tempfile.TemporaryDirectory() as td:
            spawned = []

            class FakeProcess:
                def __init__(self):
                    self.pid = 8200 + len(spawned)

            def popen(*_a, **_kw):
                spawned.append(1)
                return FakeProcess()

            manager = SubagentManager(root_dir=td, popen=popen, python_executable="python-test", sleep=lambda _: None)

            first = manager.spawn_agent("alpha", "task a", submission_id="retry_1")
            second = manager.spawn_agent("beta", "task b", submission_id="retry_1")

            self.assertEqual(len(spawned), 2, "a reused id suppressed the spawn of a different agent")
            self.assertEqual(second.agent_path, "/root/beta")
            self.assertNotEqual(second.run_id, first.run_id)

    def test_a_reused_submission_id_is_still_idempotent_per_agent(self):
        """Scoping by target must not cost the guarantee the id exists for."""
        with tempfile.TemporaryDirectory() as td:
            spawned = []

            class FakeProcess:
                def __init__(self):
                    self.pid = 8300 + len(spawned)

            def popen(*_a, **_kw):
                spawned.append(1)
                return FakeProcess()

            manager = SubagentManager(root_dir=td, popen=popen, python_executable="python-test", sleep=lambda _: None)

            manager.spawn_agent("alpha", "task a", submission_id="retry_2")
            manager.spawn_agent("beta", "task b", submission_id="retry_2")
            replay_a = manager.spawn_agent("alpha", "task a", submission_id="retry_2")
            replay_b = manager.spawn_agent("beta", "task b", submission_id="retry_2")

            self.assertEqual(len(spawned), 2, "a genuine replay started another process")
            self.assertEqual(replay_a.agent_path, "/root/alpha")
            self.assertEqual(replay_b.agent_path, "/root/beta")

    def test_the_same_submission_id_closes_each_agent_it_names(self):
        with tempfile.TemporaryDirectory() as td:
            manager = self._manager(td)
            manager.spawn_agent("alpha", "task a")
            manager.spawn_agent("beta", "task b")
            manager.terminate_process = lambda _pid: None
            manager.process_exists = lambda _pid: False

            manager.close_agent("alpha", reason="done", submission_id="cleanup")
            second = manager.close_agent("beta", reason="done", submission_id="cleanup")

            self.assertEqual(second.previous_state.agent_path, "/root/beta")
            self.assertEqual(second.closed_state.agent_path, "/root/beta")

    def test_replayed_close_agent_reports_the_first_close(self):
        with tempfile.TemporaryDirectory() as td:
            manager = self._manager(td)
            manager.spawn_agent("closing", "task")
            terminated = []
            manager.terminate_process = terminated.append
            manager.process_exists = lambda _pid: False

            first = manager.close_agent("closing", reason="done", submission_id="sub_close")
            second = manager.close_agent("closing", reason="done", submission_id="sub_close")

            self.assertEqual(second.previous_state.agent_path, first.previous_state.agent_path)
            self.assertEqual(second.closed_state.process_status, first.closed_state.process_status)

    def test_a_replayed_close_of_a_gone_agent_does_not_raise(self):
        """The replay's whole job is to be safe after the first call already finished the work."""
        with tempfile.TemporaryDirectory() as td:
            manager = self._manager(td)
            manager.spawn_agent("vanishing", "task")
            manager.process_exists = lambda _pid: False
            manager.terminate_process = lambda _pid: None
            manager.close_agent("vanishing", reason="done", submission_id="sub_gone")

            import shutil

            shutil.rmtree(Path(td) / "temp" / "vanishing")

            replay = manager.close_agent("vanishing", reason="done", submission_id="sub_gone")

            self.assertEqual(replay.previous_state.agent_path, "/root/vanishing")


class MulticastMessageTest(unittest.TestCase):
    """B2's other half: Codex `InterAgentCommunication` carries `other_recipients`; GA had no
    multicast at all, so telling three agents one thing meant three separate submissions with
    three separate ids to keep straight."""

    def _manager(self, td):
        class FakeProcess:
            next_pid = 9000

            def __init__(self):
                self.pid = FakeProcess.next_pid
                FakeProcess.next_pid += 1

        return SubagentManager(
            root_dir=td,
            popen=lambda *_, **__: FakeProcess(),
            python_executable="python-test",
            sleep=lambda _: None,
        )

    def _rows(self, td, name):
        from subagent_mailbox import SubagentMailbox

        return SubagentMailbox(Path(td) / "temp" / name / "mailbox.jsonl")._read_rows()

    def test_one_send_reaches_every_recipient(self):
        with tempfile.TemporaryDirectory() as td:
            manager = self._manager(td)
            for name in ("a", "b", "c"):
                manager.spawn_agent(name, "initial")

            result = manager.send_message("a", "shared context", author="/root", other_recipients=["b", "c"])

            for name in ("a", "b", "c"):
                rows = [row for row in self._rows(td, name) if row.get("content") == "shared context"]
                self.assertEqual(len(rows), 1, f"{name} did not get the multicast exactly once")
            self.assertEqual(result["recipient"], "/root/a")
            self.assertEqual(
                [row["recipient"] for row in result["other_deliveries"]], ["/root/b", "/root/c"]
            )

    def test_every_copy_records_who_else_received_it(self):
        """A recipient that cannot see the other recipients cannot avoid duplicating their work."""
        with tempfile.TemporaryDirectory() as td:
            manager = self._manager(td)
            for name in ("a", "b"):
                manager.spawn_agent(name, "initial")

            manager.send_message("a", "shared", author="/root", other_recipients=["b"])

            row_a = next(row for row in self._rows(td, "a") if row["content"] == "shared")
            row_b = next(row for row in self._rows(td, "b") if row["content"] == "shared")
            self.assertEqual(row_a["other_recipients"], ["/root/b"])
            self.assertEqual(row_b["other_recipients"], ["/root/a"])

    def test_a_duplicated_or_self_naming_recipient_is_delivered_once(self):
        with tempfile.TemporaryDirectory() as td:
            manager = self._manager(td)
            for name in ("a", "b"):
                manager.spawn_agent(name, "initial")

            manager.send_message("a", "once", author="/root", other_recipients=["a", "b", "b", "/root/a"])

            self.assertEqual(len([row for row in self._rows(td, "a") if row["content"] == "once"]), 1)
            self.assertEqual(len([row for row in self._rows(td, "b") if row["content"] == "once"]), 1)

    def test_a_failing_extra_recipient_does_not_lose_the_primary_delivery(self):
        with tempfile.TemporaryDirectory() as td:
            manager = self._manager(td)
            manager.spawn_agent("a", "initial")
            real_queue = manager._queue_message

            def flaky(target, message, **kwargs):
                if manager._task_name_from_target(target) == "broken":
                    raise OSError("mailbox is unwritable")
                return real_queue(target, message, **kwargs)

            manager._queue_message = flaky

            result = manager.send_message("a", "still lands", author="/root", other_recipients=["broken"])

            self.assertEqual(len([row for row in self._rows(td, "a") if row["content"] == "still lands"]), 1)
            self.assertEqual(result["other_deliveries"][0]["status"], "error")

    def test_multicast_followup_triggers_a_turn_for_every_recipient(self):
        with tempfile.TemporaryDirectory() as td:
            manager = self._manager(td)
            for name in ("a", "b"):
                manager.spawn_agent(name, "initial")

            manager.followup_task("a", "go", author="/root", other_recipients=["b"])

            for name in ("a", "b"):
                row = next(row for row in self._rows(td, name) if row["content"] == "go")
                self.assertTrue(row["trigger_turn"], f"{name} got the multicast without being woken")


class SpawnNameConflictTest(unittest.TestCase):
    """Spawning onto a live agent's name must be refused, not silently renamed.

    Measured before this existed: `spawn_agent("reviewer", ...)` while /root/reviewer was still
    running produced /root/reviewer_1 — a second OS process, a second active-agent slot and a
    second real LLM spend, while the model believed there was one agent. Codex refuses the same
    situation outright (`codex-rs/core/src/agent/registry.rs:247-250`,
    "agent path `{agent_path}` already exists"); the rename only makes sense once the previous
    agent is gone, where it protects that agent's artifacts from being overwritten.
    """

    def _manager(self, td, *, alive_pids=(), popen_log=None):
        class FakeProcess:
            next_pid = 7000

            def __init__(self):
                self.pid = FakeProcess.next_pid
                FakeProcess.next_pid += 1

        def popen(*_a, **_kw):
            if popen_log is not None:
                popen_log.append(1)
            return FakeProcess()

        return SubagentManager(
            root_dir=td,
            popen=popen,
            python_executable="python-test",
            sleep=lambda _: None,
            # Liveness decides rename-vs-refuse, so the tests must own it rather than depend on
            # whether a fake pid happens to exist on the host running them.
            process_exists=lambda pid: pid in set(alive_pids) or alive_pids == "all",
        )

    def test_spawning_over_a_live_agent_is_refused_instead_of_silently_renamed(self):
        from subagent_registry import SubagentNameConflictError

        with tempfile.TemporaryDirectory() as td:
            spawned = []
            manager = self._manager(td, alive_pids="all", popen_log=spawned)
            first = manager.spawn_agent("reviewer", "review the diff")

            with self.assertRaises(SubagentNameConflictError) as ctx:
                manager.spawn_agent("reviewer", "review it again")

            self.assertIn("/root/reviewer", str(ctx.exception))
            self.assertEqual(len(spawned), 1, "the refused spawn still started a process")
            self.assertEqual([state.agent_path for state in manager.list_agents()], [first.agent_path])

    def test_the_refusal_tells_the_model_what_to_do_instead(self):
        """A bare "already exists" makes the model retry with a mangled name; it needs the verbs."""
        from subagent_registry import SubagentNameConflictError

        with tempfile.TemporaryDirectory() as td:
            manager = self._manager(td, alive_pids="all")
            manager.spawn_agent("reviewer", "review the diff")

            with self.assertRaises(SubagentNameConflictError) as ctx:
                manager.spawn_agent("reviewer", "again")

            message = str(ctx.exception)
            self.assertIn("followup_task", message)
            self.assertIn("close_agent", message)

    def test_the_refusal_is_recorded_as_a_spawn_rejected_event(self):
        """Same reason the tree-limit refusal is recorded: an unrecorded refusal is invisible."""
        from subagent_registry import SubagentNameConflictError

        with tempfile.TemporaryDirectory() as td:
            manager = self._manager(td, alive_pids="all")
            manager.spawn_agent("reviewer", "review the diff")

            with self.assertRaises(SubagentNameConflictError):
                manager.spawn_agent("reviewer", "again")

            rejected = [
                event
                for event in manager.event_bus.read_events_since(0)
                if event.get("type") == "spawn_rejected"
            ]
            self.assertEqual(len(rejected), 1)
            self.assertIn("/root/reviewer", rejected[0]["payload"]["reason"])

    def test_spawning_over_a_closed_agent_still_renames_and_keeps_its_artifacts(self):
        with tempfile.TemporaryDirectory() as td:
            manager = self._manager(td, alive_pids=())
            first = manager.spawn_agent("reviewer", "first prompt")
            (Path(first.task_dir) / "output.txt").write_text("first answer\n\n[ROUND END]\n", encoding="utf-8")
            manager.close_agent("reviewer", reason="done", grace_s=0)

            second = manager.spawn_agent("reviewer", "second prompt")

            self.assertEqual(second.task_name, "reviewer_1")
            self.assertNotEqual(first.task_dir, second.task_dir)
            self.assertEqual(
                (Path(first.task_dir) / "output.txt").read_text(encoding="utf-8"),
                "first answer\n\n[ROUND END]\n",
                "the closed agent's artifacts were clobbered by the reused name",
            )

    def test_a_crashed_agent_does_not_block_its_name_forever(self):
        """A row nobody closed is not a live agent; refusing here would strand the name.

        Renamed rather than reused because the crashed agent's task dir is the only remaining
        evidence of what it did — the same reason _reap_stale_agents closes rows instead of
        deleting them.
        """
        with tempfile.TemporaryDirectory() as td:
            manager = self._manager(td, alive_pids=())
            first = manager.spawn_agent("reviewer", "first prompt")
            (Path(first.task_dir) / "output.txt").write_text("crashed midway\n", encoding="utf-8")

            second = manager.spawn_agent("reviewer", "second prompt")

            self.assertEqual(second.task_name, "reviewer_1")
            self.assertEqual((Path(first.task_dir) / "output.txt").read_text(encoding="utf-8"), "crashed midway\n")

    def test_a_nested_agent_is_not_refused_because_an_unrelated_root_agent_shares_the_name(self):
        """The name is scoped to the spawning agent's path, so a live /root/x must not refuse /root/a/x.

        The nested agent still gets a suffixed *name*: task dirs are flat (temp/<task_name>), so
        two agents at different depths cannot share one directory. That is pre-existing layout,
        not the conflict guard — what matters here is that the spawn is allowed at all.
        """
        with tempfile.TemporaryDirectory() as td:
            manager = self._manager(td, alive_pids="all")
            manager.spawn_agent("worker", "root level")
            child = SubagentManager(
                root_dir=td,
                popen=lambda *_a, **_kw: type("P", (), {"pid": 7500})(),
                python_executable="python-test",
                sleep=lambda _: None,
                process_exists=lambda _pid: True,
                self_agent_path="/root/parent",
            )

            nested = child.spawn_agent("worker", "nested level")

            self.assertTrue(
                nested.agent_path.startswith("/root/parent/"),
                f"nested spawn landed outside its parent: {nested.agent_path}",
            )


class ResumeAgentGuardTest(unittest.TestCase):
    """`resume_agent` is the one control-plane op that starts a process without any guard.

    Measured (temp/_probe_resume_live.py):
      - resuming an agent whose process is still alive launched a *second* process and overwrote
        `state.json`'s pid with it, so the first pid was no longer referenced anywhere — nothing
        could wait on it, interrupt it or close it, and it kept spending real LLM budget.
      - two `resume_agent` calls for the same logical submission launched two processes; unlike
        spawn/close/followup (B2) resume never took a `submission_id`, so a replay was unguarded.
    """

    def _manager(self, td, *, alive, popen_log=None):
        class FakeProcess:
            next_pid = 6000

            def __init__(self):
                self.pid = FakeProcess.next_pid
                FakeProcess.next_pid += 1

        def popen(*_a, **_kw):
            proc = FakeProcess()
            if popen_log is not None:
                popen_log.append(proc.pid)
            return proc

        return SubagentManager(
            root_dir=td,
            popen=popen,
            python_executable="python-test",
            sleep=lambda _: None,
            process_exists=lambda _pid: alive,
        )

    def _closed_agent(self, td, *, alive=False, popen_log=None):
        manager = self._manager(td, alive=alive, popen_log=popen_log)
        handle = manager.spawn_agent("worker", "initial", parent_session_id="sess_resume")
        manager.terminate_process = lambda _pid: None
        manager.close_agent("worker", reason="done", grace_s=0)
        return manager

    def test_resuming_a_still_running_agent_is_refused_instead_of_orphaning_its_process(self):
        from subagent_registry import SubagentNameConflictError

        with tempfile.TemporaryDirectory() as td:
            launched = []
            manager = self._manager(td, alive=True, popen_log=launched)
            handle = manager.spawn_agent("worker", "initial", parent_session_id="sess_resume")

            with self.assertRaises(SubagentNameConflictError) as ctx:
                manager.resume_agent("worker", "keep going")

            self.assertEqual(launched, [handle.pid], "the refused resume still launched a process")
            self.assertEqual(json.loads(Path(handle.state_path).read_text(encoding="utf-8"))["pid"], handle.pid)
            self.assertIn("followup_task", str(ctx.exception))

    def test_resuming_a_closed_agent_still_works(self):
        with tempfile.TemporaryDirectory() as td:
            launched = []
            manager = self._closed_agent(td, popen_log=launched)

            result = manager.resume_agent("worker", "keep going")

            self.assertEqual(result.handle.task_name, "worker")
            self.assertEqual(len(launched), 2, "resume of a closed agent must still start a process")

    def test_a_replayed_resume_does_not_start_a_second_process(self):
        with tempfile.TemporaryDirectory() as td:
            launched = []
            manager = self._closed_agent(td, popen_log=launched)

            first = manager.resume_agent("worker", "keep going", submission_id="sub_resume")
            second = manager.resume_agent("worker", "keep going", submission_id="sub_resume")

            self.assertEqual(len(launched), 2, "a replayed resume started a second real process")
            self.assertEqual(second.handle.pid, first.handle.pid)
            self.assertEqual(second.handle.run_id, first.handle.run_id)
            self.assertEqual(second.target, first.target)

    def test_without_a_submission_id_a_second_resume_is_still_a_second_round(self):
        """Two deliberate resumes are legitimate; only an id says "this is the same submission"."""
        with tempfile.TemporaryDirectory() as td:
            launched = []
            manager = self._closed_agent(td, popen_log=launched)

            manager.resume_agent("worker", "round one")
            manager.terminate_process = lambda _pid: None
            manager.close_agent("worker", reason="done", grace_s=0)
            manager.resume_agent("worker", "round two")

            self.assertEqual(len(launched), 3)


if __name__ == "__main__":
    unittest.main()
