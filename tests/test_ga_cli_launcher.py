import unittest
from unittest.mock import patch

from ga_cli import cli


class GaCliLauncherTest(unittest.TestCase):
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
            with patch("ga_cli.cli.subprocess.Popen", side_effect=lambda cmd: popen_calls.append(cmd) or Proc()):
                cli.launch_frontend(["npm", "--version"])

        self.assertEqual("npm.cmd", popen_calls[0][0])

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
        self.assertIn("ink-ui/src/main.tsx", cmd[2].replace("\\", "/"))


if __name__ == "__main__":
    unittest.main()
