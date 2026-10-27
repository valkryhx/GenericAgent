import os
import tempfile
import unittest
from pathlib import Path
from io import StringIO
from unittest.mock import patch

from ga_cli import cli


class GaCliLauncherTest(unittest.TestCase):
    def setUp(self):
        self._td = tempfile.TemporaryDirectory()
        self.addCleanup(self._td.cleanup)

    def test_resolve_executable_uses_shutil_which_for_command_name(self):
        with patch("ga_cli.cli.shutil.which", return_value=r"C:\nodejs\npm.CMD"):
            self.assertEqual(r"C:\nodejs\npm.CMD", cli.resolve_executable("npm"))

    def test_resolve_executable_leaves_paths_unchanged(self):
        self.assertEqual(r".\local-tool.cmd", cli.resolve_executable(r".\local-tool.cmd"))
        self.assertEqual(r"C:\Tools\tool.exe", cli.resolve_executable(r"C:\Tools\tool.exe"))

    def test_launch_frontend_resolves_first_command_before_popen(self):
        popen_calls = []

        class Proc:
            def wait(self):
                return 0

            def terminate(self):
                pass

        with patch("ga_cli.cli.resolve_executable", side_effect=lambda part: "npm.cmd" if part == "npm" else part):
            with patch("ga_cli.cli.subprocess.Popen", side_effect=lambda cmd, **kwargs: popen_calls.append(cmd) or Proc()):
                cli.launch_frontend(["npm", "--version"])

        self.assertEqual("npm.cmd", popen_calls[0][0])

    def test_launch_frontend_is_silent_by_default(self):
        class Proc:
            def wait(self):
                return 0

            def terminate(self):
                pass

        output = StringIO()

        with patch("ga_cli.cli.subprocess.Popen", return_value=Proc()):
            with patch("sys.stdout", output):
                cli.launch_frontend(["python", "--version"])

        self.assertEqual("", output.getvalue())

    def test_main_without_command_launches_default_ink_frontend(self):
        launched = []

        with patch("sys.argv", ["ga"]):
            with patch("ga_cli.cli.launch_frontend", side_effect=lambda cmd, args=None: launched.append((cmd, args))):
                cli.main()

        self.assertEqual(cli.COMMANDS["ink"]["cmd"], launched[0][0])
        self.assertIsNone(launched[0][1])

    def test_ink_command_bypasses_npm_script_for_tty_input(self):
        cmd = cli.COMMANDS["ink"]["cmd"]

        self.assertEqual("node", cmd[0])
        self.assertIn("node_modules/tsx/dist/cli.mjs", cmd[1].replace("\\", "/"))
        self.assertIn("ink/ui/src/main.tsx", cmd[2].replace("\\", "/"))

    def test_supported_entry_points_are_ga_and_ink(self):
        self.assertEqual({"ink", "cli", "list", "status", "update", "migrate"}, set(cli.COMMANDS))

    def test_legacy_ui_commands_are_not_advertised(self):
        for name in ("gui", "web", "launch", "tui", "pet", "hub", "configure"):
            self.assertIn(name, cli.DEPRECATED_COMMANDS)
            self.assertNotIn(name, cli.COMMANDS)

    def test_deprecated_command_exits_with_actionable_message(self):
        output = StringIO()

        with patch("sys.argv", ["ga", "tui"]):
            with patch("sys.stdout", output):
                with self.assertRaises(SystemExit) as raised:
                    cli.main()

        self.assertEqual(2, raised.exception.code)
        self.assertIn("[DEPRECATED] tui:", output.getvalue())
        self.assertIn("ga ink", output.getvalue())

    def test_launch_frontend_preserves_workspace_for_bridge(self):
        seen = {}

        class Proc:
            def wait(self):
                return 0

            def terminate(self):
                pass

        def fake_popen(cmd, **kwargs):
            seen.update(kwargs)
            seen["cwd_during_launch"] = os.getcwd()
            return Proc()

        original = os.getcwd()
        fake_frontends = os.path.join(original, "frontends")
        with patch("ga_cli.cli.subprocess.Popen", side_effect=fake_popen):
            with patch("ga_cli.cli._frontends", return_value=fake_frontends):
                cli.launch_frontend(["python", "--version"])

        self.assertEqual(original, os.getcwd())
        self.assertEqual(cli.PROJECT_DIR, seen["env"]["GA_PROJECT_ROOT"])
        self.assertEqual(os.path.join(fake_frontends, "ink", "ui"), seen["cwd_during_launch"])

    def test_launcher_prefers_user_project_cwd_over_source_checkout(self):
        with tempfile.TemporaryDirectory() as td:
            project = Path(td) / "user-project"
            nested = project / "src"
            nested.mkdir(parents=True)
            (project / "pyproject.toml").write_text("[project]" + chr(10) + "name='user-project'" + chr(10), encoding="utf-8")
            source = Path(td) / "GenericAgent-source"
            source.mkdir()
            with patch("ga_cli.cli._source_project_dir", return_value=str(source)):
                with patch("ga_cli.cli.os.getcwd", return_value=str(nested)):
                    with patch.dict(os.environ, {"GA_PROJECT_ROOT": ""}, clear=False):
                        self.assertEqual(str(project), cli._runtime_project_dir())

    def test_launcher_uses_source_checkout_when_launched_from_source(self):
        with tempfile.TemporaryDirectory() as td:
            source = Path(td) / "GenericAgent-source"
            nested = source / "frontends"
            nested.mkdir(parents=True)
            (source / "pyproject.toml").write_text("[project]" + chr(10) + "name='ga'" + chr(10), encoding="utf-8")
            with patch("ga_cli.cli._source_project_dir", return_value=str(source)):
                with patch("ga_cli.cli.os.getcwd", return_value=str(nested)):
                    with patch.dict(os.environ, {"GA_PROJECT_ROOT": ""}, clear=False):
                        self.assertEqual(str(source), cli._runtime_project_dir())

    def test_installed_launcher_uses_caller_cwd(self):
        caller = os.getcwd()
        with patch("ga_cli.cli._source_project_dir", return_value=""):
            with patch.dict(os.environ, {"GA_PROJECT_ROOT": ""}, clear=False):
                self.assertEqual(caller, cli._runtime_project_dir())


    def test_blank_directory_is_a_valid_project_root(self):
        blank = Path(self._td.name) / "blank-project"
        blank.mkdir()
        source = Path(self._td.name) / "GenericAgent-source"
        source.mkdir()

        with patch("ga_cli.cli._source_project_dir", return_value=str(source)):
            with patch("ga_cli.cli.os.getcwd", return_value=str(blank)):
                with patch.dict(os.environ, {"GA_PROJECT_ROOT": ""}, clear=False):
                    self.assertEqual(str(blank), cli._runtime_project_dir())

        snapshot = cli.project_environment(project=blank, launch=blank, data_root=Path(self._td.name, "data"))
        self.assertEqual(str(blank), snapshot["GA_PROJECT_ROOT"])
        self.assertTrue(snapshot["GA_PROJECT_KEY"])
        self.assertIn(str(Path(self._td.name, "data", "projects")), snapshot["GA_PROJECT_DATA_DIR"])

    def test_launch_frontend_injects_project_identity_snapshot(self):
        seen = {}

        class Proc:
            def wait(self):
                return 0

            def terminate(self):
                pass

        def fake_popen(cmd, **kwargs):
            seen.update(kwargs["env"])
            return Proc()

        frontends = os.path.join(self._td.name, "frontends")
        launch_cwd = os.path.join(self._td.name, "project", "src", "deep")
        with patch("ga_cli.cli.subprocess.Popen", side_effect=fake_popen):
            with patch("ga_cli.cli._frontends", return_value=frontends):
                with patch("ga_cli.cli.PROJECT_DIR", os.path.join(self._td.name, "project")):
                    with patch("ga_cli.cli.os.getcwd", return_value=launch_cwd):
                        cli.launch_frontend(["python", "--version"])

        self.assertEqual(launch_cwd, seen["GA_LAUNCH_CWD"])
        self.assertEqual(launch_cwd, seen["GA_ORIGINAL_CWD"])
        self.assertEqual(os.path.join(self._td.name, "project"), seen["GA_PROJECT_ROOT"])
        self.assertIn("GA_PROJECT_KEY", seen)
        self.assertIn("GA_PROJECT_DATA_DIR", seen)
        self.assertIn("GA_WORKTREE_ROOT", seen)

    def test_project_identity_snapshot_is_stable_for_subdir_launch(self):
        project = Path(self._td.name, "project")
        nested = project / "src" / "deep"
        (project / ".git").mkdir(parents=True)
        nested.mkdir(parents=True)
        first = cli.project_environment(project=project, launch=nested, data_root=Path(self._td.name, "data"))
        second = cli.project_environment(project=project, launch=nested / "further", data_root=Path(self._td.name, "data"))
        self.assertEqual(first["GA_PROJECT_KEY"], second["GA_PROJECT_KEY"])
        self.assertEqual(first["GA_PROJECT_DATA_DIR"], second["GA_PROJECT_DATA_DIR"])

if __name__ == "__main__":
    unittest.main()
