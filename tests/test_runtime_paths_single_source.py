import tempfile
import unittest
from pathlib import Path

from frontends.ink.bridge import GenericAgentBridge
from genericagent import paths
from genericagent.agents.manager import SubagentManager


class _Agent:
    session_id = "session_single_source"
    project_root = None
    data_root = None
    log_path = None


class RuntimePathsSingleSourceTest(unittest.TestCase):
    def test_runtime_paths_contract_matches_project_roots(self):
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "A"
            data = Path(td) / "data"
            project.mkdir()
            roots = paths.project_roots(project=project, data_root=data)
            runtime = paths.runtime_paths(roots=roots, session_id="session_1")
            self.assertEqual(roots.project_root, runtime.project_root)
            self.assertEqual(roots.project_data_dir, runtime.project_data_dir)
            self.assertEqual(roots.project_worktrees_dir, runtime.worktree_root)
            self.assertEqual(roots.project_workflows_dir, runtime.project_workflows_dir)
            self.assertEqual(roots.user_workflows_dir, runtime.user_workflows_dir)
            self.assertEqual(roots.legacy_workflows_dir, runtime.legacy_workflows_dir)

    def test_bridge_rebinds_all_roots_from_agent_identity(self):
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "A"
            data = Path(td) / "data"
            project.mkdir()
            agent = _Agent()
            agent.project_root = str(project)
            agent.data_root = str(data)
            bridge = GenericAgentBridge.__new__(GenericAgentBridge)
            bridge.agent = agent
            bridge.project_dir = Path(td)
            bridge.state_root = Path(td) / "unused"
            bridge._workspace_root_override = None
            bridge._workflow_root_override = None
            bridge._rebind_runtime_paths()
            bridge._build_agent_control()
            roots = paths.project_roots(project=project, data_root=data)
            self.assertEqual(roots.project_root, bridge.project_dir)
            self.assertEqual(roots.data_root, bridge.state_root)
            self.assertEqual(roots.project_data_dir, bridge.project_roots.project_data_dir)
            self.assertEqual(roots.project_data_dir, bridge.workflow_store.root)
            self.assertEqual(
                roots.project_data_dir / "session_single_source" / "subagents",
                bridge.subagent_manager.session_data_dir,
            )

    def test_manager_project_layout_does_not_use_legacy_temp_fallback(self):
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "A"
            data = Path(td) / "data"
            project.mkdir()
            roots = paths.project_roots(project=project, data_root=data)
            manager = SubagentManager(
                project_root=project,
                project_state_root=data,
                project_data_dir=roots.project_data_dir,
                parent_session_id="session_1",
                task_store_root=roots.tasks_dir,
                worktree_root=roots.project_worktrees_dir,
            )
            self.assertEqual(roots.project_data_dir / "session_1" / "subagents", manager.session_data_dir)
            self.assertEqual(roots.project_data_dir / "session_1" / "subagents" / "tasks", manager.task_root)
            self.assertNotEqual(data / "temp" / "sessions", manager.session_data_dir)


if __name__ == "__main__":
    unittest.main()
