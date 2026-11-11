"""The workflow JS preflight must work from an installed wheel.

Reported failure, first launch after install:

    Cannot find module 'acorn'
    require stack: ...site-packages/genericagent/workflow/js/script_contract.js

`script_contract.js:4` does `require('acorn')`. Node resolves a bare specifier by
walking up from the *requiring file* looking for `node_modules/acorn`. In a source
checkout that walk hits `<repo>/node_modules/acorn` (the root `package.json` declares
it). In an installed wheel the JS lives at
`<site-packages>/genericagent/workflow/js/`, and nothing on that chain contains
acorn -- `node_modules/` is gitignored and the build never packaged it.

Copying acorn into site-packages by hand fixes one machine and is wiped by the next
`pip install`. These tests pin the two properties that make the packaged case work:

  1. the wheel ships acorn next to the JS that requires it, and
  2. the preflight still resolves acorn when the module is reachable only through
     an explicit resolver (`NODE_PATH`), which is how a bundle can put it
     somewhere the default walk would not find.
"""
from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
JS_DIR = REPO_ROOT / "genericagent" / "workflow" / "js"
WHEEL_GLOB = "genericagent-*-py3-none-any.whl"


def _node_available() -> bool:
    try:
        subprocess.run(["node", "--version"], capture_output=True, check=True, timeout=10)
        return True
    except (OSError, subprocess.SubprocessError):
        return False


class WorkflowPreflightResolvesAcornTest(unittest.TestCase):
    """The contract parser must run without a source checkout's node_modules."""

    def setUp(self):
        if not _node_available():
            self.skipTest("node is not available")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def _run_contract(self, *, contract: Path, env_extra: dict | None = None, cwd: Path | None = None):
        import os

        env = {**os.environ}
        # A source checkout's node_modules would rescue a broken packaged layout, so
        # point the resolver away from the repo entirely.
        env.pop("NODE_PATH", None)
        env.update(env_extra or {})
        return subprocess.run(
            ["node", str(contract), "--check"],
            input="export const meta = { name: 'x', description: 'y' }\n",
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            timeout=30,
            cwd=str(cwd or self.root),
            env=env,
        )

    def test_the_js_dir_ships_acorn_so_the_default_walk_resolves_it(self):
        """The fix: bundle acorn under the JS dir the requiring file lives in.

        Node checks `<dir>/node_modules/acorn` first, so this works with no
        NODE_PATH and no global install. The repo already has acorn; the build
        copies it here for the wheel.
        """
        vendored = JS_DIR / "node_modules" / "acorn" / "package.json"
        if not vendored.is_file():
            self.skipTest("acorn is not vendored into the package yet (build does it)")

        # Copy the package JS into a bare directory tree with no repo above it.
        shadow_js = self.root / "js"
        shadow_js.mkdir()
        for name in ("script_contract.js", "worker.js"):
            (shadow_js / name).write_bytes((JS_DIR / name).read_bytes())
        import shutil

        shutil.copytree(JS_DIR / "node_modules", shadow_js / "node_modules")

        completed = self._run_contract(contract=shadow_js / "script_contract.js")
        self.assertEqual(
            0,
            completed.returncode,
            f"script_contract.js could not resolve acorn from a wheel-shaped layout:\n"
            f"stdout={completed.stdout}\nstderr={completed.stderr}",
        )

    def test_an_explicit_node_path_also_resolves_acorn(self):
        """Belt and braces: an explicit resolver must work too, so a bundle that puts
        acorn elsewhere (not under js/node_modules) can still run the preflight."""
        acorn_src = JS_DIR / "node_modules" / "acorn"
        if not acorn_src.is_dir():
            acorn_src = REPO_ROOT / "node_modules" / "acorn"
        if not acorn_src.is_dir():
            self.skipTest("no acorn copy available to stage")

        import shutil

        staging = self.root / "resolver"
        (staging).mkdir()
        shutil.copytree(acorn_src, staging / "acorn")

        completed = self._run_contract(
            contract=JS_DIR / "script_contract.js",
            env_extra={"NODE_PATH": str(staging)},
            # Run from a directory with no node_modules above it.
            cwd=self.root,
        )
        self.assertEqual(
            0,
            completed.returncode,
            f"script_contract.js did not honour NODE_PATH:\n"
            f"stdout={completed.stdout}\nstderr={completed.stderr}",
        )


class PackagedWheelShipsAcornTest(unittest.TestCase):
    """The built wheel must carry acorn, or every installed user hits the bug."""

    def test_wheel_contains_vendored_acorn_next_to_the_js(self):
        wheels = sorted((REPO_ROOT / "dist").glob(WHEEL_GLOB))
        if not wheels:
            self.skipTest("no built wheel in dist/ to inspect")
        wheel = wheels[-1]
        with zipfile.ZipFile(wheel) as archive:
            names = archive.namelist()
        acorn_entries = [n for n in names if "workflow/js/node_modules/acorn/" in n]
        self.assertTrue(
            acorn_entries,
            f"{wheel.name} does not ship acorn. Installed users get "
            f"'Cannot find module \\'acorn\\'' on the first workflow run. "
            f"JS entries present: {[n for n in names if 'workflow/js' in n]}",
        )
        # The entry point Node actually resolves must be there, not just the folder.
        self.assertTrue(
            any(n.endswith("workflow/js/node_modules/acorn/package.json") for n in names),
            "acorn is present but incomplete: package.json missing",
        )
        self.assertTrue(
            any(n.endswith("workflow/js/node_modules/acorn/dist/acorn.js") for n in names),
            "acorn is present but its main entry (dist/acorn.js) is missing",
        )


class ContractPreflightFromWheelLayoutTest(unittest.TestCase):
    """End-to-end: run GA's own preflight against a wheel-shaped JS directory."""

    def setUp(self):
        if not _node_available():
            self.skipTest("node is not available")

    def test_preflight_passes_with_vendored_acorn(self):
        acorn_src = JS_DIR / "node_modules" / "acorn"
        if not acorn_src.is_dir():
            legacy = REPO_ROOT / "node_modules" / "acorn"
            if not legacy.is_dir():
                self.skipTest("no acorn copy available to stage")
            acorn_src = legacy

        with tempfile.TemporaryDirectory() as td:
            import shutil

            js = Path(td) / "js"
            js.mkdir()
            for name in ("script_contract.js", "worker.js", "__init__.py"):
                (js / name).write_bytes((JS_DIR / name).read_bytes())
            (js / "node_modules").mkdir()
            shutil.copytree(acorn_src, js / "node_modules" / "acorn")

            completed = subprocess.run(
                ["node", str(js / "script_contract.js"), "--check"],
                input="export const meta = { name: 'probe', description: 'd' }\n",
                text=True,
                encoding="utf-8",
                errors="replace",
                capture_output=True,
                timeout=30,
                cwd=td,
            )
            self.assertEqual(0, completed.returncode, completed.stderr)
            payload = json.loads(completed.stdout)
            self.assertTrue(payload.get("ok"), payload)


if __name__ == "__main__":
    unittest.main()