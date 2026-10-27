import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from genericagent.core import transcripts as session_transcript


class FakeAgent:
    def __init__(self):
        self.session_id = None
        self.session_path = None
        self.session_turn_id = 0


class SessionIdentityTest(unittest.TestCase):
    def test_create_session_writes_additive_project_identity(self):
        with tempfile.TemporaryDirectory() as td:
            identity = {
                "original_cwd": str(Path(td) / "project"),
                "project_root": str(Path(td) / "project"),
                "launch_cwd": str(Path(td) / "project" / "src"),
                "project_key": "project-abc123",
                "data_root": str(Path(td) / "data"),
                "layout": "project-v1",
            }
            path = session_transcript.create_session(
                root=Path(td) / "sessions",
                cwd=str(Path(td) / "project"),
                session_id="session_identity",
                identity=identity,
                schema=2,
            )

            event = session_transcript.load_session(path).events[0]
            self.assertEqual(identity["original_cwd"], event["original_cwd"])
            self.assertEqual(identity["project_root"], event["project_root"])
            self.assertEqual(identity["launch_cwd"], event["launch_cwd"])
            self.assertEqual(identity["project_key"], event["project_key"])
            self.assertEqual(identity["data_root"], event["data_root"])
            self.assertEqual("project-v1", event["layout"])

    def test_loaded_session_exposes_identity_without_cwd_guessing(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            project = root / "project"
            identity = {
                "original_cwd": str(project),
                "project_root": str(project),
                "launch_cwd": str(project / "src"),
                "project_key": "project-abc123",
                "data_root": str(root / "data"),
                "layout": "project-v1",
            }
            path = session_transcript.create_session(
                root=root / "sessions",
                cwd=str(root / "other"),
                session_id="session_identity",
                identity=identity,
                schema=2,
            )

            loaded = session_transcript.load_session(path)
            self.assertEqual(identity["project_root"], loaded.identity.project_root)
            self.assertEqual(identity["project_key"], loaded.identity.project_key)

    def test_legacy_session_has_no_inferred_project_identity(self):
        with tempfile.TemporaryDirectory() as td:
            path = session_transcript.create_session(
                root=td, cwd="C:/looks-like-project", session_id="session_legacy", schema=1
            )
            loaded = session_transcript.load_session(path)
            self.assertIsNone(loaded.identity)


    def test_ensure_agent_session_writes_into_project_bucket_when_identity_exists(self):
        agent = FakeAgent()
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "project"
            project.mkdir()
            data = Path(td) / "data"
            identity = {
                "original_cwd": str(project),
                "project_root": str(project),
                "launch_cwd": str(project),
                "data_root": str(data),
                "layout": "project-v1",
            }
            path = session_transcript.ensure_agent_session(agent, identity=identity)
            expected = data.resolve() / "projects" / __import__("genericagent.paths", fromlist=["project_key"]).project_key(project)
            self.assertEqual(expected / f"{agent.session_id}.jsonl", Path(path))
            self.assertTrue(Path(path).is_file())

    def test_ensure_agent_session_records_environment_identity(self):
        agent = FakeAgent()
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "project"
            project.mkdir()
            env = {
                "GA_PROJECT_ROOT": str(project),
                "GA_LAUNCH_CWD": str(project / "src"),
                "GA_ORIGINAL_CWD": str(project / "src"),
                "GA_DATA_DIR": str(Path(td) / "data"),
            }
            with mock.patch.dict(os.environ, env, clear=False):
                path = session_transcript.ensure_agent_session(agent, root=Path(td) / "sessions")
            loaded = session_transcript.load_session(path)
            self.assertIsNotNone(loaded.identity)
            self.assertEqual(str(project), loaded.identity.project_root)
            self.assertEqual(str(project / "src"), loaded.identity.launch_cwd)
            self.assertEqual(str(project), agent.project_root)
            expected_data = (Path(td) / "data").resolve() / "projects" / __import__("genericagent.paths", fromlist=["project_key"]).project_key(project)
            self.assertEqual(str(Path(td) / "data"), agent.data_root)
            self.assertEqual(expected_data, Path(agent.project_data_dir))


class AgentStartupIdentityTest(unittest.TestCase):
    def test_agent_startup_uses_environment_identity_and_project_bucket(self):
        from genericagent.core.app import GenericAgent
        from genericagent import paths

        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "Alpha"
            project.mkdir()
            data = Path(td) / "data"
            env = {
                "GA_PROJECT_ROOT": str(project),
                "GA_DATA_DIR": str(data),
                "GA_LAUNCH_CWD": str(project / "src"),
                "GA_ORIGINAL_CWD": str(project / "src"),
            }
            old_init = GenericAgent.__init__

            def init_identity_only(self):
                self.session_id = None
                self.session_path = None
                self.session_turn_id = 0
                self.history = []
                self.llmclient = None
                identity = session_transcript.identity_from_environment()
                self.original_cwd = identity.get("original_cwd")
                self.project_root = identity.get("project_root")
                self.launch_cwd = identity.get("launch_cwd")
                self.project_key = identity.get("project_key")
                self.data_root = identity.get("data_root")
                self.tool_cwd = self.project_root
                session_transcript.ensure_agent_session(self, identity=identity)

            try:
                GenericAgent.__init__ = init_identity_only
                with mock.patch.dict(os.environ, env, clear=False):
                    agent = GenericAgent()
            finally:
                GenericAgent.__init__ = old_init
            self.assertTrue(Path(agent.session_path).is_file())
            self.assertEqual(project.resolve(), Path(agent.project_root))
            self.assertEqual(project.resolve(), Path(agent.tool_cwd))
            expected = data.resolve() / "projects" / paths.project_key(project)
            self.assertEqual(expected, Path(agent.session_path).parent)
            self.assertEqual(expected, Path(agent.project_data_dir))


if __name__ == "__main__":
    unittest.main()
