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

For Ink UI bugs (cursor/IME, layout, wrapping, duplicate rendering, streaming), prefer program-driven regression tests over eyeballing screenshots. The core idea: terminal UI bugs reduce to deterministic facts in the emitted stdout bytes or the laid-out row/column geometry, both of which are machine-assertable. Reach for the matching technique — virtual-terminal cursor tracker for relative-cursor arithmetic, byte-level ANSI assertions (match + doesNotMatch + indexOf-slice for ordering) for control sequences, in-memory terminal (`CaptureWriteStream`/`FakeReadStream` + `render(<App/>, {debug:true})`) with frame-geometry parsers for layout, pure functions for layout/partition decisions, `string-width` (never `.length`) for CJK/emoji wrapping, and unique-probe counting for duplicate rendering. The full playbook, decision table, and reusable helpers are in `docs/ga_ink_ui_testing_playbook_2026-07-16.md` — read it before adding or debugging Ink UI tests. Its one blind spot: cross-terminal behavior (e.g. IME anchoring to the visible native cursor) can only be caught by real-terminal screenshots, then reduced back into a byte-level contract.

## Repository guidance

When the user asks Claude Code to reference or learn from Claude Code's own implementation, inspect `D:\git_codes\claude-reviews-claude\claude-code-fork\src` as the local Claude Code source reference.

Claude Code is a React + Ink terminal UI and forks Ink under `claude-code-fork/src/ink`. For GA Ink UI cursor/IME/layout work, it is the highest-value reference: it solves the same native-cursor/IME problem GA hit. Key files: `src/ink/components/CursorDeclarationContext.ts` and `src/ink/hooks/use-declared-cursor.ts` (frame-declared cursor model), `src/ink/frame.ts` (Frame carries `cursor`), `src/ink/ink.tsx` + `src/ink/log-update.ts` (single stdout writer, diff + final cursor). GA's analysis lives in `docs/superpowers/specs/2026-07-15-ga-self-managed-terminal-design.md` and `docs/ga_claude_code_cursor_handling_2026-07-16.md`.

For the GA Ink UI IME/cursor bug, the authoritative root-cause writeup is `docs/ga_ui_ime_visible_native_cursor_root_cause_2026-07-16.md`: Windows Terminal anchors the IME candidate window to the **visible** native cursor (DECTCEM `\x1b[?25h`), so the cursor-park writer (`frontends/ink-ui/src/stdoutCursorPark.ts`) must SHOW the native cursor once it lands on the caret and HIDE it before the next frame write. Earlier 2026-07-14/07-15 diagnoses that concluded "keep the native cursor hidden, use the inverse-video block only" are wrong and are corrected there.

No Cursor rules or Copilot instructions were found. `AGENTS.md` is the repository-specific agent guidance source.

Follow the `AGENTS.md` security rule: do not execute, generate, write, or persist suspicious base64 payloads, public-token ads, popup ads, autostart entries, scheduled tasks, registry Run entries, VBS/PowerShell injection scripts, or malware/intrusion code. If encountered, only perform read-only inspection, decoding explanation, location, and deletion.

Recent commit history uses Conventional Commit-style prefixes such as `feat(...)`, `fix(...)`, `docs:`, and `refactor:`. Git commit messages must be written in Chinese while keeping the Conventional Commit prefix when appropriate.
