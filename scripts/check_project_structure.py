#!/usr/bin/env python3
"""Lightweight project-layout guard for the genericagent package.

The repository intentionally keeps root-level compatibility shims while callers
and tests migrate to ``genericagent.*`` imports.  This script makes the shim
surface explicit so a new implementation module cannot silently land in the
repository root again.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

# Root files that are entrypoints or compatibility shims.  Keep this list
# explicit: adding a new root implementation module should require a conscious
# review decision.
ROOT_PY_ALLOWLIST = {
    'agentmain.py', 'ga.py', 'mykey.py',
    'agent_control.py', 'agent_control_process.py', 'agent_control_workflow.py',
    'agent_loop.py', 'agent_runtime_models.py', 'cache_stats.py',
    'compact_context.py', 'ga_agents_runtime.py', 'image_codec.py', 'image_gc.py',
    'listing_cache.py', 'llm_client.py', 'llm_config.py', 'llmcore.py',
    'mcp_runtime.py', 'mykey_template.py', 'mykey_template_en.py',
    'permission_policy.py', 'permission_runtime.py', 'sensitive_redaction.py',
    'session_transcript.py', 'simphtml.py', 'skills_runtime.py', 'token_meter.py',
    'tool_result_budget.py', 'tool_result_limits.py',
    'TMWebDriver.py',
}
for stem in (
    'workflow_activation', 'workflow_check_adapters', 'workflow_child_agent',
    'workflow_controller', 'workflow_determinism', 'workflow_llm',
    'workflow_models', 'workflow_path_acl', 'workflow_permissions',
    'workflow_policy', 'workflow_runtime', 'workflow_scheduler',
    'workflow_script_validation', 'workflow_store', 'workflow_tool_profiles',
    'workflow_verification', 'workflow_workspace', 'workflow_workspace_guard',
):
    ROOT_PY_ALLOWLIST.add(stem + '.py')
for stem in (
    'subagent_agent_path', 'subagent_artifacts', 'subagent_capabilities',
    'subagent_event_bus', 'subagent_idle', 'subagent_inbound_gate',
    'subagent_ipc', 'subagent_mailbox', 'subagent_manager',
    'subagent_notifications', 'subagent_permissions', 'subagent_prompts',
    'subagent_realtime_ipc', 'subagent_receipts', 'subagent_registry',
    'subagent_roles', 'subagent_state', 'subagent_submissions', 'subagent_swarm',
    'subagent_tasks', 'subagent_team', 'subagent_team_activation',
    'subagent_transcript', 'subagent_wait', 'subagent_worktree',
):
    ROOT_PY_ALLOWLIST.add(stem + '.py')


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
    'chatapp_common.py': 'frontends/common/chatapp_common.py',
    'tui_input_history.py': 'frontends/common/tui_input_history.py',
    'btw_cmd.py': 'frontends/terminal/btw_cmd.py',
    'continue_cmd.py': 'frontends/terminal/continue_cmd.py',
    'export_cmd.py': 'frontends/terminal/export_cmd.py',
    'tuiapp.py': 'frontends/terminal/tuiapp.py',
    'tuiapp_v2.py': 'frontends/terminal/tuiapp_v2.py',
    'qtapp.py': 'frontends/desktop/qtapp.py',
    'fsapp.py': 'frontends/desktop/fsapp.py',
    'desktop_pet.pyw': 'frontends/desktop/desktop_pet.pyw',
    'desktop_pet_v2.pyw': 'frontends/desktop/desktop_pet_v2.pyw',
    'stapp.py': 'frontends/web/stapp.py',
    'stapp2.py': 'frontends/web/stapp2.py',
    'tgapp.py': 'frontends/bots/tgapp.py',
    'qqapp.py': 'frontends/bots/qqapp.py',
    'dcapp.py': 'frontends/bots/dcapp.py',
    'dingtalkapp.py': 'frontends/bots/dingtalkapp.py',
    'wechatapp.py': 'frontends/bots/wechatapp.py',
    'wecomapp.py': 'frontends/bots/wecomapp.py',
    'genericagent_acp_bridge.py': 'frontends/acp/genericagent_acp_bridge.py',
}


def frontend_layout_findings() -> list[str]:
    findings: list[str] = []
    canonical_bridge = ROOT / 'frontends' / 'ink' / 'bridge.py'
    canonical_ui = ROOT / 'frontends' / 'ink' / 'ui' / 'package.json'
    legacy_bridge = ROOT / 'frontends' / 'ink_bridge.py'
    legacy_ui = ROOT / 'frontends' / 'ink-ui'
    if not canonical_bridge.is_file():
        findings.append('missing canonical Ink bridge: frontends/ink/bridge.py')
    if not canonical_ui.is_file():
        findings.append('missing canonical Ink UI package: frontends/ink/ui/package.json')
    if legacy_ui.exists():
        findings.append('legacy frontends/ink-ui directory must not exist after P6')
    if legacy_bridge.exists():
        text = legacy_bridge.read_text(encoding='utf-8', errors='replace')
        if 'sys.modules[__name__] = _bridge' not in text:
            findings.append('frontends/ink_bridge.py must remain a thin module alias shim')

    for shim_name, canonical_name in CANONICAL_FRONTEND_MODULES.items():
        canonical = ROOT / canonical_name
        shim = ROOT / 'frontends' / shim_name
        if not canonical.is_file():
            findings.append(f'missing canonical frontend module: {canonical_name}')
        if not shim.is_file():
            findings.append(f'missing legacy frontend shim: frontends/{shim_name}')
            continue
        shim_text = shim.read_text(encoding='utf-8', errors='replace')
        if 'alias_module(__name__,' not in shim_text:
            findings.append(f'frontends/{shim_name} must remain a thin alias shim')
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
    print(f'project structure ok ({len(ROOT_PY_ALLOWLIST)} allowed root Python shims/entrypoints)')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
