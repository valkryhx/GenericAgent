import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


from subagent_agent_path import AgentPath  # noqa: E402
from subagent_registry import SubagentRegistry  # noqa: E402


class SubagentRegistryTest(unittest.TestCase):
    def test_create_child_assigns_canonical_path_run_id_and_artifact_dir(self):
        with tempfile.TemporaryDirectory() as td:
            registry = SubagentRegistry(Path(td) / "temp" / "subagents")

            entry = registry.create_child(
                parent_path=AgentPath.root(),
                task_name="researcher",
                task_dir=Path(td) / "temp" / "researcher",
                state_path=Path(td) / "temp" / "researcher" / "state.json",
                pid=1234,
                parent_session_id="session_demo",
                last_task_message="do research",
            )

            self.assertEqual(str(entry.agent_path), "/root/researcher")
            self.assertEqual(str(entry.parent_path), "/root")
            self.assertTrue(entry.run_id.startswith("run_"))
            self.assertEqual(entry.task_name, "researcher")
            self.assertEqual(entry.pid, 1234)
            self.assertEqual(entry.parent_session_id, "session_demo")
            self.assertEqual(entry.last_task_message, "do research")
            self.assertIn(entry.run_id, entry.artifact_dir)
            self.assertIn("runs", entry.artifact_dir)
            self.assertTrue(registry.path.exists())

    def test_duplicate_task_name_creates_unique_child_path_and_does_not_reuse_artifact_dir(self):
        with tempfile.TemporaryDirectory() as td:
            registry = SubagentRegistry(Path(td) / "temp" / "subagents")

            first = registry.create_child(
                parent_path="/root",
                task_name="researcher",
                task_dir=Path(td) / "temp" / "researcher",
                state_path=Path(td) / "temp" / "researcher" / "state.json",
            )
            second = registry.create_child(
                parent_path="/root",
                task_name="researcher",
                task_dir=Path(td) / "temp" / "researcher_1",
                state_path=Path(td) / "temp" / "researcher_1" / "state.json",
            )

            self.assertEqual(str(first.agent_path), "/root/researcher")
            self.assertEqual(str(second.agent_path), "/root/researcher_1")
            self.assertNotEqual(first.run_id, second.run_id)
            self.assertNotEqual(first.artifact_dir, second.artifact_dir)
            self.assertEqual([str(e.agent_path) for e in registry.list_agents(include_closed=True)], [
                "/root/researcher",
                "/root/researcher_1",
            ])

    def test_list_agents_filters_by_prefix_and_closed_status(self):
        with tempfile.TemporaryDirectory() as td:
            registry = SubagentRegistry(Path(td) / "temp" / "subagents")
            researcher = registry.create_child("/root", "researcher", Path(td) / "temp" / "researcher", Path(td) / "temp" / "researcher" / "state.json")
            worker = registry.create_child(researcher.agent_path, "worker", Path(td) / "temp" / "worker", Path(td) / "temp" / "worker" / "state.json")
            closed = registry.mark_closed(worker.agent_path, previous_status="running", closed_status="shutdown")

            self.assertEqual(closed.previous_status, "running")
            self.assertEqual(closed.status, "closed")
            self.assertEqual([str(e.agent_path) for e in registry.list_agents(path_prefix="/root/researcher")], [
                "/root/researcher",
            ])
            self.assertEqual([str(e.agent_path) for e in registry.list_agents(path_prefix="/root/researcher", include_closed=True)], [
                "/root/researcher",
                "/root/researcher/worker",
            ])

    def test_update_status_and_get_round_trips_from_disk(self):
        with tempfile.TemporaryDirectory() as td:
            registry = SubagentRegistry(Path(td) / "temp" / "subagents")
            entry = registry.create_child("/root", "researcher", Path(td) / "temp" / "researcher", Path(td) / "temp" / "researcher" / "state.json")

            registry.update(entry.agent_path, pid=5678, turn_status="completed", process_status="waiting_reply")
            reloaded = SubagentRegistry(Path(td) / "temp" / "subagents")
            got = reloaded.get("/root/researcher")

            self.assertEqual(got.pid, 5678)
            self.assertEqual(got.turn_status, "completed")
            self.assertEqual(got.process_status, "waiting_reply")


class RegistryDescendantsTest(unittest.TestCase):
    """`close_agent(cascade=true)` needs to know who lives below a path.

    The v2 design lists `descendants()` in the registry API (§6 schema + P0 test list), but
    nothing ever implemented it. Without it, closing a middle-tier agent orphans its children:
    they keep running, keep consuming the G1 active-agent cap, and only get reaped once their
    process dies — the exact failure mode the stale-row reaping had to clean up by hand.
    """

    def _registry(self, td, **kwargs):
        return SubagentRegistry(Path(td) / "temp" / "subagents", **kwargs)

    def _child(self, registry, parent, name, td):
        return registry.create_child(parent, name, Path(td) / "temp" / name, Path(td) / "temp" / name / "state.json")

    def test_descendants_excludes_the_agent_itself_and_returns_deepest_first(self):
        """Deepest-first is the close order: a leaf must go before the parent that owns it."""
        with tempfile.TemporaryDirectory() as td:
            registry = self._registry(td, max_depth=0)
            top = self._child(registry, "/root", "top", td)
            mid = self._child(registry, top.agent_path, "mid", td)
            leaf = self._child(registry, mid.agent_path, "leaf", td)

            paths = [str(e.agent_path) for e in registry.descendants(top.agent_path)]

            self.assertEqual(paths, [str(leaf.agent_path), str(mid.agent_path)])

    def test_descendants_does_not_match_a_sibling_sharing_the_name_prefix(self):
        """/root/a must not sweep up /root/ab — a string prefix is not a path prefix."""
        with tempfile.TemporaryDirectory() as td:
            registry = self._registry(td)
            first = self._child(registry, "/root", "a", td)
            self._child(registry, "/root", "ab", td)
            child = self._child(registry, first.agent_path, "inner", td)

            self.assertEqual([str(e.agent_path) for e in registry.descendants("/root/a")], [str(child.agent_path)])

    def test_descendants_skips_closed_rows_unless_asked(self):
        with tempfile.TemporaryDirectory() as td:
            registry = self._registry(td)
            top = self._child(registry, "/root", "top", td)
            gone = self._child(registry, top.agent_path, "gone", td)
            alive = self._child(registry, top.agent_path, "alive", td)
            registry.mark_closed(gone.agent_path, previous_status="running", closed_status="shutdown")

            self.assertEqual([str(e.agent_path) for e in registry.descendants(top.agent_path)], [str(alive.agent_path)])
            self.assertEqual(
                sorted(str(e.agent_path) for e in registry.descendants(top.agent_path, include_closed=True)),
                [str(alive.agent_path), str(gone.agent_path)],
            )

    def test_descendants_of_a_leaf_is_empty(self):
        with tempfile.TemporaryDirectory() as td:
            registry = self._registry(td)
            leaf = self._child(registry, "/root", "leaf", td)

            self.assertEqual(registry.descendants(leaf.agent_path), [])


class SubagentTreeLimitsTest(unittest.TestCase):
    """G1: nothing bounded the agent tree, and every GA subagent is a separate OS process.

    A child can spawn children of its own, so an unbounded tree burns processes, memory and
    real LLM spend. Codex guards the same thing with AgentRegistry { active_agents, total_count }
    and reserve_spawn_slot before the spawn.
    """

    def _registry(self, td, **kwargs):
        return SubagentRegistry(Path(td) / "temp" / "subagents", **kwargs)

    def _child(self, registry, parent, name, td):
        return registry.create_child(parent, name, Path(td) / "temp" / name, Path(td) / "temp" / name / "state.json")

    def test_depth_limit_rejects_a_grandchild_beyond_the_cap(self):
        from subagent_registry import SubagentTreeLimitError

        with tempfile.TemporaryDirectory() as td:
            registry = self._registry(td, max_depth=2)
            first = self._child(registry, "/root", "a", td)
            second = self._child(registry, first.agent_path, "b", td)

            with self.assertRaises(SubagentTreeLimitError) as ctx:
                self._child(registry, second.agent_path, "c", td)

            self.assertIn("depth", str(ctx.exception).lower())
            self.assertIn("2", str(ctx.exception))
            self.assertEqual(len(registry.list_agents()), 2)

    def test_active_agent_limit_rejects_one_too_many(self):
        from subagent_registry import SubagentTreeLimitError

        with tempfile.TemporaryDirectory() as td:
            registry = self._registry(td, max_active_agents=2)
            self._child(registry, "/root", "a", td)
            second = self._child(registry, "/root", "b", td)

            with self.assertRaises(SubagentTreeLimitError) as ctx:
                self._child(registry, "/root", "c", td)

            self.assertIn("active", str(ctx.exception).lower())

            # Closing one frees a slot: the cap is on live agents, not on lifetime spawns.
            registry.mark_closed(second.agent_path, previous_status="running", closed_status="shutdown")
            revived = self._child(registry, "/root", "c", td)
            self.assertEqual(str(revived.agent_path), "/root/c")

    def test_limits_default_to_conservative_values_and_are_env_overridable(self):
        from subagent_registry import resolve_tree_limits_from_env

        with tempfile.TemporaryDirectory() as td:
            registry = self._registry(td)
            self.assertEqual(registry.max_depth, 3)
            self.assertEqual(registry.max_active_agents, 8)

        self.assertEqual(
            resolve_tree_limits_from_env({"GA_SUBAGENT_MAX_DEPTH": "5", "GA_SUBAGENT_MAX_ACTIVE": "20"}),
            {"max_depth": 5, "max_active_agents": 20},
        )
        self.assertEqual(resolve_tree_limits_from_env({}), {})
        self.assertEqual(resolve_tree_limits_from_env({"GA_SUBAGENT_MAX_DEPTH": "0"}), {})


class NestedSpawnDepthTest(unittest.TestCase):
    """The depth cap only bites if nesting is actually recorded as nesting.

    spawn_agent() hard-coded AgentPath.root() as the parent, so a grandchild registered as
    /root/<name> at depth 1 and the tree looked flat no matter how deep it really went.
    Children run agentmain.py against the same repo root and therefore the same registry,
    so the child process has to be told which agent it is.
    """

    def _manager(self, td, **kwargs):
        from subagent_manager import SubagentManager

        return SubagentManager(
            root_dir=td,
            popen=kwargs.pop("popen", lambda *_, **__: type("FakeProcess", (), {"pid": 4242})()),
            python_executable="python-test",
            process_exists=lambda _pid: True,
            sleep=lambda _: None,
            **kwargs,
        )

    def test_spawn_records_the_spawning_agent_as_the_parent(self):
        with tempfile.TemporaryDirectory() as td:
            manager = self._manager(td, self_agent_path="/root/parent_agent")

            handle = manager.spawn_agent("nested_child", "go")

            self.assertEqual(handle.agent_path, "/root/parent_agent/nested_child")

    def test_spawned_child_is_told_its_own_agent_path_via_the_environment(self):
        with tempfile.TemporaryDirectory() as td:
            captured = {}

            def popen(cmd, **kwargs):
                captured.update(kwargs)
                return type("FakeProcess", (), {"pid": 4242})()

            manager = self._manager(td, popen=popen)
            handle = manager.spawn_agent("env_child", "go")

            self.assertEqual(captured["env"]["GA_SUBAGENT_AGENT_PATH"], handle.agent_path)
            self.assertIn("PATH", captured["env"], "child env must extend the parent env, not replace it")

    def test_manager_reads_its_own_identity_from_the_environment(self):
        from subagent_manager import SubagentManager

        with tempfile.TemporaryDirectory() as td:
            with unittest.mock.patch.dict("os.environ", {"GA_SUBAGENT_AGENT_PATH": "/root/a/b"}):
                manager = SubagentManager(root_dir=td, python_executable="python-test")

            self.assertEqual(str(manager.self_agent_path), "/root/a/b")

    def test_rejected_spawn_is_recorded_on_the_event_bus(self):
        """A refused spawn that leaves no trace is indistinguishable from one never attempted."""
        with tempfile.TemporaryDirectory() as td:
            manager = self._manager(td, self_agent_path="/root/a/b/c")
            manager.registry.max_depth = 3

            with self.assertRaises(Exception):
                manager.spawn_agent("too_deep", "go")

            events = manager.event_bus.read_events_since(0)
            rejected = [e for e in events if e["type"] == "spawn_rejected"]
            self.assertEqual(len(rejected), 1)
            self.assertEqual(rejected[0]["task_name"], "too_deep")
            self.assertIn("depth", rejected[0]["payload"]["reason"].lower())

    def test_depth_cap_rejects_a_spawn_past_the_limit_with_a_clear_reason(self):
        with tempfile.TemporaryDirectory() as td:
            manager = self._manager(td, self_agent_path="/root/a/b/c")
            manager.registry.max_depth = 3

            with self.assertRaises(Exception) as ctx:
                manager.spawn_agent("too_deep", "go")

            self.assertIn("depth", str(ctx.exception).lower())


class StaleActiveAgentReapingTest(unittest.TestCase):
    """G1 follow-up, found by the real-API E2E: the cap counted rows, not live processes.

    `_check_tree_limits` treated every row whose status was not "closed" as active, but a
    row only becomes "closed" when close_agent runs. Crashes, kills and machine reboots all
    leave the row behind, so a long-lived repo accumulates them until the cap refuses every
    spawn — the real registry had 36 "active" rows of which exactly 1 process was alive, and
    the terra E2E could not start at all. A cap that has to be reset by hand is not a guard.
    """

    def _registry(self, td, **kwargs):
        return SubagentRegistry(Path(td) / "temp" / "subagents", **kwargs)

    def _child(self, registry, parent, name, td, **kwargs):
        return registry.create_child(
            parent, name, Path(td) / "temp" / name, Path(td) / "temp" / name / "state.json", **kwargs
        )

    def test_rows_whose_process_is_gone_do_not_consume_active_slots(self):
        with tempfile.TemporaryDirectory() as td:
            registry = self._registry(td, max_active_agents=2, process_exists=lambda pid: pid == 777)
            self._child(registry, "/root", "crashed", td, pid=111)
            self._child(registry, "/root", "still_running", td, pid=777)

            survivor = self._child(registry, "/root", "newcomer", td, pid=888)

            self.assertEqual(str(survivor.agent_path), "/root/newcomer")

    def test_a_reaped_row_is_closed_with_a_distinguishable_status_not_deleted(self):
        """The row is evidence of a crash; closing it keeps the audit trail that deleting loses."""
        with tempfile.TemporaryDirectory() as td:
            registry = self._registry(td, max_active_agents=8, process_exists=lambda _pid: False)
            crashed = self._child(registry, "/root", "crashed", td, pid=111)

            self._child(registry, "/root", "newcomer", td, pid=888)

            reaped = registry.get(crashed.agent_path)
            self.assertEqual(reaped.status, "closed")
            self.assertEqual(reaped.closed_status, "stale")
            self.assertTrue(reaped.closed_at)

    def test_a_row_with_no_pid_yet_is_still_active(self):
        """spawn registers the child before Popen, so pid=None means "starting", not "dead"."""
        with tempfile.TemporaryDirectory() as td:
            from subagent_registry import SubagentTreeLimitError

            registry = self._registry(td, max_active_agents=1, process_exists=lambda _pid: False)
            self._child(registry, "/root", "not_launched_yet", td)

            with self.assertRaises(SubagentTreeLimitError):
                self._child(registry, "/root", "second", td)

    def test_an_unreliable_process_probe_counts_the_row_as_alive_and_does_not_crash(self):
        """psutil can raise on a pid we have no rights to. "Can't tell" must mean "alive".

        The alternative — reaping on a failed probe — would mark a live agent's row closed,
        dropping it out of list_agents/wait_agents. That trades a guard problem for a
        correctness problem, so the probe failure is contained but the row is kept.
        """
        with tempfile.TemporaryDirectory() as td:
            from subagent_registry import SubagentTreeLimitError

            def flaky(_pid):
                raise OSError("access denied")

            registry = self._registry(td, max_active_agents=2, process_exists=flaky)
            self._child(registry, "/root", "unknowable", td, pid=111)
            self._child(registry, "/root", "second", td, pid=222)

            with self.assertRaises(SubagentTreeLimitError):
                self._child(registry, "/root", "third", td, pid=333)

            self.assertEqual([str(e.agent_path) for e in registry.list_agents()], ["/root/second", "/root/unknowable"])

    def test_manager_gives_the_registry_its_own_liveness_probe(self):
        from subagent_manager import SubagentManager

        with tempfile.TemporaryDirectory() as td:
            probe = lambda _pid: False  # noqa: E731
            manager = SubagentManager(root_dir=td, process_exists=probe, python_executable="python-test")

            self.assertIs(manager.registry.process_exists, probe)


if __name__ == "__main__":
    unittest.main()
