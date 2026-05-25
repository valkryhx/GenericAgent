# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Common commands

### Python setup and tests

```bash
python -m pip install -e .
python -m pip install -e ".[ui]"
python -m unittest discover -s tests
python -m unittest tests.test_ink_bridge
python -m unittest tests.test_ink_bridge.InkBridgeTest.test_resume_transcript_uses_exact_backend_history_before_each_turn_for_rewind
```

### Run GenericAgent frontends

```bash
ga
ga ink
ga cli
ga list
python launch.pyw
python frontends/tuiapp.py
streamlit run frontends/stapp2.py
```

`ga` with no subcommand starts the React/Ink terminal UI by default. `ga ink` uses `frontends/ink-ui` through the Python JSONL bridge. `ga cli` runs the plain Python CLI.

### Ink UI setup and checks

Run npm commands from `frontends/ink-ui` rather than the repository root.

```bash
cd frontends/ink-ui
npm install
npm run test
npm run typecheck
npm run start
npx tsx --test src/markdownRender.test.ts
```

On Windows, `install_ink_ui.cmd` installs the Ink UI dependencies from the repository root.

## Architecture overview

GenericAgent is a compact Python autonomous agent runtime with multiple frontends.

- `agentmain.py` owns `GenericAgent`: task queue, model/session selection, cancellation, slash commands, and transcript initialization.
- `agent_loop.py` contains the LLM turn loop. `agent_runner_loop()` sends messages to the client, parses tool calls, dispatches them through handler methods, and feeds tool results back into the next turn.
- `ga.py` contains local tool implementations and `GenericAgentHandler` behavior. Tools follow the `do_<tool_name>` dispatch pattern from `agent_loop.BaseHandler`.
- `llmcore.py` loads `mykey.py` or `mykey.json`, expands provider/profile config, and abstracts provider clients, streaming parsing, and history/context behavior.
- `assets/tools_schema*.json` defines static tool schemas. `assets/sys_prompt*.txt` provides the base system prompt.
- `ga_cli/cli.py` implements the `ga` command dispatcher and launches GUI, web, TUI, Ink, and CLI frontends from the repository root.

## Frontends and bridge

Most UI adapters live under `frontends/`. The React/Ink UI is intentionally independent from the Textual frontend:

- `frontends/ink-ui` is a React 18 + Ink 5 terminal UI run by `tsx`.
- `frontends/ink_bridge.py` exposes a JSONL stdio protocol so the Node/Ink process can drive `GenericAgent` without importing Textual UI code.
- The bridge redirects backend stdout/stderr to `temp/ink_bridge_backend.log` so stdout remains valid JSONL protocol output.

## Persistence, compaction, MCP, and skills

- `session_transcript.py` records resumable JSONL sessions under `temp/sessions` with `session_start`, `turn`, `compact`, and `rewind` events.
- `compact_context.py` estimates backend history size and compacts old context when it exceeds the configured threshold.
- `mcp_runtime.py` optionally loads MCP servers from `mcp.json` or `GA_MCP_CONFIG`, normalizing exposed tool names as `mcp__<server>__<tool>`.
- `skills_runtime.py` discovers Claude/Codex-style `SKILL.md` packages from `~/.claude/skills`, `~/.codex/skills`, and `GA_SKILL_PATHS`.
- `memory/` stores long-term memory, SOPs, and helper modules. `temp/` stores generated logs, sessions, transcripts, and runtime artifacts.

## Configuration

Create `mykey.py` or `mykey.json` from the provided templates before using real LLM providers. The README documents `mykey_template.py`, `mykey_template_en.py`, and runtime `/model` or `/llm` switching commands.

## Testing conventions

Python tests use the standard library `unittest` and live under `tests/` as `test_*.py`. Stub external services and avoid real credentials in tests.

Ink UI tests live beside the TypeScript sources as `frontends/ink-ui/src/*.test.ts` and run with Node's test runner through `tsx`.

## Repository guidance

When the user asks Claude Code to reference or learn from Claude Code's own implementation, inspect `D:\git_codes\claude-reviews-claude\claude-code-fork\src` as the local Claude Code source reference.

No Cursor rules or Copilot instructions were found. `AGENTS.md` is the repository-specific agent guidance source.

Follow the `AGENTS.md` security rule: do not execute, generate, write, or persist suspicious base64 payloads, public-token ads, popup ads, autostart entries, scheduled tasks, registry Run entries, VBS/PowerShell injection scripts, or malware/intrusion code. If encountered, only perform read-only inspection, decoding explanation, location, and deletion.

Recent commit history uses Conventional Commit-style prefixes such as `feat(...)`, `fix(...)`, `docs:`, and `refactor:`.
