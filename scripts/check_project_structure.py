#!/usr/bin/env python3
"""Lightweight project-layout guard for the genericagent package.

Runtime code lives in canonical package paths.  The repository root is reserved
for the local ``mykey.py`` configuration file, and frontend implementations live
under their runtime-shaped subpackages.  This guard prevents the tree from
silently flattening again.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# The root is reserved for the local configuration file only.  Runtime code
# and compatibility shims must live in their canonical package locations.
ROOT_PY_ALLOWLIST = {'mykey.py'}


def root_implementation_files() -> list[Path]:
    return sorted(
        p for p in ROOT.glob('*.py')
        if p.name not in ROOT_PY_ALLOWLIST
    )


def path_guess_findings() -> list[str]:
    findings: list[str] = []
    pattern = re.compile(r'Path\(__file__\)\.resolve\(\)\.parent\s*/\s*["\']temp["\']')
    for path in sorted((ROOT / 'genericagent').rglob('*.py')):
        text = path.read_text(encoding='utf-8', errors='replace')
        if pattern.search(text):
            findings.append(f'{path.relative_to(ROOT)}: use genericagent.paths instead of local temp guessing')
    return findings


CANONICAL_FRONTEND_MODULES = {
    'frontends/common/chatapp_common.py',
    'frontends/common/tui_input_history.py',
    'frontends/terminal/btw_cmd.py',
    'frontends/terminal/continue_cmd.py',
    'frontends/terminal/export_cmd.py',
    'frontends/terminal/tuiapp.py',
    'frontends/terminal/tuiapp_v2.py',
    'frontends/desktop/qtapp.py',
    'frontends/desktop/fsapp.py',
    'frontends/desktop/desktop_pet.pyw',
    'frontends/desktop/desktop_pet_v2.pyw',
    'frontends/web/stapp.py',
    'frontends/web/stapp2.py',
    'frontends/bots/tgapp.py',
    'frontends/bots/qqapp.py',
    'frontends/bots/dcapp.py',
    'frontends/bots/dingtalkapp.py',
    'frontends/bots/wechatapp.py',
    'frontends/bots/wecomapp.py',
    'frontends/acp/genericagent_acp_bridge.py',
}


def frontend_layout_findings() -> list[str]:
    findings: list[str] = []
    canonical_bridge = ROOT / 'frontends' / 'ink' / 'bridge.py'
    canonical_ui = ROOT / 'frontends' / 'ink' / 'ui' / 'package.json'
    if not canonical_bridge.is_file():
        findings.append('missing canonical Ink bridge: frontends/ink/bridge.py')
    if not canonical_ui.is_file():
        findings.append('missing canonical Ink UI package: frontends/ink/ui/package.json')

    for name in sorted(CANONICAL_FRONTEND_MODULES):
        if not (ROOT / name).is_file():
            findings.append(f'missing canonical frontend module: {name}')

    legacy_frontends = sorted(
        p.relative_to(ROOT).as_posix()
        for p in (ROOT / 'frontends').glob('*.py')
        if p.name != '__init__.py'
    )
    legacy_frontends += sorted(
        p.relative_to(ROOT).as_posix()
        for p in (ROOT / 'frontends').glob('*.pyw')
    )
    if legacy_frontends:
        findings.append(
            'legacy top-level frontend shims must not exist: '
            + ', '.join(legacy_frontends)
        )
    return findings


def main() -> int:
    failures: list[str] = []
    unexpected = root_implementation_files()
    if unexpected:
        failures.append(
            'unexpected root implementation modules: '
            + ', '.join(p.name for p in unexpected)
        )
    failures.extend(path_guess_findings())
    failures.extend(frontend_layout_findings())
    if failures:
        for item in failures:
            print(f'FAIL: {item}')
        return 1
    print('project structure ok (root allowlist: mykey.py; canonical frontends present)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
