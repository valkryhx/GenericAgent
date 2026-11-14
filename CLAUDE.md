# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Common commands

### Python setup and tests

```bash
python -m pip install -e .
python -m unittest discover -s tests -t .
python -m unittest tests.test_ink_bridge
python -m unittest tests.test_ink_bridge.InkBridgeTest.test_resume_transcript_uses_exact_backend_history_before_each_turn_for_rewind
```

### Run GenericAgent frontends

```bash
ga
ga ink
ga cli
ga list
```

`ga` with no subcommand starts the React/Ink terminal UI by default. `ga ink` uses `frontends/ink/ui` through the Python JSONL bridge. `ga cli` runs the plain Python CLI.

Textual, Qt, Streamlit, pywebview, desktop-pet, Hub, and bot adapters are deprecated legacy surfaces. Do not add them back to the default dependency set; see `docs/20261022-GA入口收敛与旧前端退役说明.md`.

### Ink UI setup and checks

Run npm commands from `frontends/ink/ui` rather than the repository root.

```bash
cd frontends/ink/ui
npm install
npm run test
npm run typecheck
npm run start
npx tsx --test src/markdownRender.test.ts
```

On Windows, `install_ink_ui.cmd` installs the Ink UI dependencies from the repository root.

## Architecture overview

GenericAgent is a compact Python autonomous agent runtime with multiple frontends.

- `genericagent/core/app.py` owns `GenericAgent`: task queue, model/session selection, cancellation, slash commands, and transcript initialization. Launch it with `python -m genericagent.core.app`.
- `genericagent/core/loop.py` contains the LLM turn loop. `agent_runner_loop()` sends messages to the client, parses tool calls, dispatches them through handler methods, and feeds tool results back into the next turn.
- `genericagent/core/handler.py` contains local tool implementations and `GenericAgentHandler` behavior. Tools follow the `do_<tool_name>` dispatch pattern from `genericagent.core.loop.BaseHandler`.
- `genericagent/llm/core.py` loads `mykey.py` or `mykey.json`, expands provider/profile config, and abstracts provider clients, streaming parsing, and history/context behavior.
- `assets/tools_schema*.json` defines static tool schemas. `assets/sys_prompt*.txt` provides the base system prompt.
- `ga_cli/cli.py` implements the `ga` command dispatcher and launches GUI, web, TUI, Ink, and CLI frontends from the repository root.

## Frontends and bridge

Most UI adapters live under `frontends/`. The React/Ink UI is intentionally independent from the Textual frontend:

- `frontends/ink/ui` is a React 18 + Ink 5 terminal UI run by `tsx`.
- `frontends/ink/bridge.py` exposes a JSONL stdio protocol so the Node/Ink process can drive `GenericAgent` without importing Textual UI code.
- The bridge redirects backend stdout/stderr to `temp/ink_bridge_backend.log` so stdout remains valid JSONL protocol output.

## Persistence, compaction, MCP, and skills

- `genericagent/core/transcripts.py` records resumable JSONL sessions under `temp/sessions` with `session_start`, `turn`, `compact`, and `rewind` events.
- `genericagent/core/context.py` estimates backend history size and compacts old context when it exceeds the configured threshold.
- `genericagent/mcp/runtime.py` optionally loads MCP servers from `mcp.json` or `GA_MCP_CONFIG`, normalizing exposed tool names as `mcp__<server>__<tool>`.
- `genericagent/core/skills.py` discovers Claude/Codex-style `SKILL.md` packages from `~/.claude/skills`, `~/.codex/skills`, and `GA_SKILL_PATHS`.
- `memory/` stores long-term memory, SOPs, and helper modules. `temp/` stores generated logs, sessions, transcripts, and runtime artifacts.

## Configuration

Create `mykey.py` or `mykey.json` from the provided templates before using real LLM providers. The README documents `genericagent/llm/templates/mykey_template.py`, `genericagent/llm/templates/mykey_template_en.py`, and runtime `/model` or `/llm` switching commands.

## Testing conventions

Python tests use the standard library `unittest` and live under `tests/` as `test_*.py`. Stub external services and avoid real credentials in tests.

Ink UI tests live beside the TypeScript sources as `frontends/ink/ui/src/*.test.ts` and run with Node's test runner through `tsx`.

### Hard rules (do not relearn these the hard way)

**1. Never delete a diagnostic script. Move it into `probes/`.**

When you write a throwaway file to find a root cause (`.test.ts` with `console.log`, a temp
script, etc.), the root cause is found **once** — deleting the script throws away the only
reproducible evidence for it. Move it instead:

| What | Where |
| --- | --- |
| Ink UI / TypeScript | `frontends/ink/ui/probes/` |
| PTY / screen capture / pyte replay (Python) | `tests/probes/python/` |
| Probe output (logs, raw bytes) | `tests/probes/captures/` (gitignored) |

Strip the `node:test` wrapper so it runs as `npx tsx probes/xxx.ts`. Put four things in the
header comment: what it measures, the root cause (`file:line`), why it is deterministic, and
**why it must be kept**. See `docs/20261115-诊断文件与诊断经验留存.md` and
`frontends/ink/ui/probes/ready_race.ts` for the model.

**2. A probe that only reproduces by luck is not evidence.**

Measuring the real timing and hoping to catch a race produces `failures=0` runs that prove
nothing — a green run cannot distinguish "fixed" from "not triggered". Build the failing
condition explicitly and include a control group (e.g. `stale-order` reproduces / 4,
`correct-order` stays clean / 4), and have the script print its own verdict (`✔` / `✖`).

**3. Wait primitives must throw on timeout.**

In tests, a `waitUntil` / `waitForFrame` that silently returns on timeout is a false-green
channel: the caller "succeeds" without the condition ever holding, and the failure surfaces
at a distant assertion. Worse, a `waitForFrame` that returns the last (often empty) frame
makes `assert.doesNotMatch('', /x/)` pass unconditionally. Always throw with the thing that
was awaited: `throw new Error(\`timed out after ${ms}ms waiting for: ${what}\`)`. Set the
timeout as a "is it actually stuck" bound (15s under a 90-file parallel run), not a latency
budget.

**4. In Ink test harnesses, wait for `ready` before emitting turn events.**

`applyBridgeEvent` resets `status` to `idle` (and clears `activityLabel`, `tokenUsage`) on
`ready` (`src/state.ts`). Harnesses commonly schedule `ready` on a timer while the test emits
`running` as soon as `onEvent` is captured — the late `ready` then wipes the state and the
assertion fails far from the cause. Set a `readyEmitted` flag **after** the emit and await it
before emitting `running`. Probe: `frontends/ink/ui/probes/ready_race.ts`.

**5. Before trusting a probe's output, check it is stable across repeats.**

A probe that samples a **non-deterministic** behavior once produces confident garbage. Real
case: `probes/resize_delay_sweep.ts` first ran each delay value once and reported
`32/48/80/120` safe; a second run reported `0/16/48/120` safe — no overlap. Re-running each
value N times showed the behavior itself is stochastic at small delays (48ms → 7/10, 64ms →
4/10, 120ms → 12/12). The fix is to report a **pass rate over N rounds**, never a single
sample, and to treat "the metric is non-monotonic in the parameter" as a sign that the
parameter is not the real variable.

**5b. When verifying that a time window disappeared, the observation window must extend past
every later suspect.**

Same round, a second and worse version of rule 5: `resize_delay_sweep` reported every delay
6/6 after a fix, and that was taken as "the ~140ms blank window is gone". It was not — that
probe watches only `delayMs + 600` and judges the **final** screen, so it cannot see a stale
frame landing near t+120ms. `probes/resize_blank_window.ts` (continuous sampling of the whole
timeline) showed 3 of 5 runs losing history at t+73~126ms. Sampling count is not enough; the
window has to cover everything that could undo the thing you just measured.

**6. When a fix is a timing heuristic, its reasoning belongs in a probe, not only a comment.**

`RESIZE_REMOUNT_DELAY_MS = 120` exists because ink's throttled tail frame replays a
pre-resize output. `probes/resize_write_timeline.ts` captures the exact mechanism: in a
failing round, a frame containing static output lands and is then overwritten by an
`eraseLines(n)` frame that writes only the live region; in a passing round the stale frame
lands first and the static remount happens afterwards. Because the outcome depends on that
**ordering**, not on the delay's magnitude, dropping the delay below 120ms has no reliable
safe value — shrinking the ~140ms blank window needs a different mechanism, not a smaller
constant.

**6b. How far that mechanism has been tried, and where it stops.**

Making `cursorPark.resetViewport` buffer the reset so it merges with the repaint fixes only
half the problem (the reset no longer appears alone). The stale throttled frame still lands
afterwards and erases the history — measured in `probes/resize_write_dump.ts`, and the
repo-wide experiment is written up in
`docs/20261115-CC为何没有resize空白窗口.md` §4.5. The wrapper can see that write but cannot
know which history rows it should have contained without touching `computeLineDiff` /
`recognizeFrame` contracts, which broke three unrelated tests. **Do not retry "just merge in
cursorPark" as if it were new** — read §4.5 first; removing the 120ms needs the stale frame's
source (ink's `throttledLog`) handled too. CC avoids this entirely because `E$o` merges a
frame's ops into one write, so no two-phase write exists.

For Ink UI bugs (cursor/IME, layout, wrapping, duplicate rendering, streaming), prefer program-driven regression tests over eyeballing screenshots. The core idea: terminal UI bugs reduce to deterministic facts in the emitted stdout bytes or the laid-out row/column geometry, both of which are machine-assertable. Reach for the matching technique — virtual-terminal cursor tracker for relative-cursor arithmetic, byte-level ANSI assertions (match + doesNotMatch + indexOf-slice for ordering) for control sequences, in-memory terminal (`CaptureWriteStream`/`FakeReadStream` + `render(<App/>, {debug:true})`) with frame-geometry parsers for layout, pure functions for layout/partition decisions, `string-width` (never `.length`) for CJK/emoji wrapping, and unique-probe counting for duplicate rendering. The full playbook, decision table, and reusable helpers are in `docs/ga_ink_ui_testing_playbook_2026-07-16.md` — read it before adding or debugging Ink UI tests. Its one blind spot: cross-terminal behavior (e.g. IME anchoring to the visible native cursor) can only be caught by real-terminal screenshots, then reduced back into a byte-level contract.
For real end-to-end self-tests against a real LLM API (a recurring need), follow `docs/20261015-GA-ink-真实LLM-e2e-自测SOP.md`: three tiers (L1 event-level `tests/real_ink_selftest.py`, L2 UI-layout `frontends/ink/ui/scripts/real_ink_team_panel_e2e.ts`, L3 interactive `./ga`), judged by evidence (event stream / logs / artifacts / real UI frames) + invariant assertions, never by fixed expected output. Note `scripts/real_ink_ui_user_input_e2e.ts` is luna-profile hardwired and fails on the default `deepseek-flash` model (subagent never reaches succeeded) — do not use it to judge the default model.


## Repository guidance

When the user asks Claude Code to reference or learn from Claude Code's own implementation, inspect `D:\git_codes\claude-reviews-claude\claude-code-fork\src` as the local Claude Code source reference.

Claude Code is a React + Ink terminal UI and forks Ink under `claude-code-fork/src/ink`. For GA Ink UI cursor/IME/layout work, it is the highest-value reference: it solves the same native-cursor/IME problem GA hit. Key files: `src/ink/components/CursorDeclarationContext.ts` and `src/ink/hooks/use-declared-cursor.ts` (frame-declared cursor model), `src/ink/frame.ts` (Frame carries `cursor`), `src/ink/ink.tsx` + `src/ink/log-update.ts` (single stdout writer, diff + final cursor). GA's analysis lives in `docs/superpowers/specs/2026-07-15-ga-self-managed-terminal-design.md` and `docs/ga_claude_code_cursor_handling_2026-07-16.md`.

For the GA Ink UI IME/cursor bug, the authoritative root-cause writeup is `docs/ga_ui_ime_visible_native_cursor_root_cause_2026-07-16.md`: Windows Terminal anchors the IME candidate window to the **visible** native cursor (DECTCEM `\x1b[?25h`), so the cursor-park writer (`frontends/ink/ui/src/stdoutCursorPark.ts`) must SHOW the native cursor once it lands on the caret and HIDE it before the next frame write. Earlier 2026-07-14/07-15 diagnoses that concluded "keep the native cursor hidden, use the inverse-video block only" are wrong and are corrected there.

When the user asks to reference or learn from Codex's own implementation — subagent lifecycle, TUI/stdout ownership, scrollback/selection, terminal draw/cursor, or related control-plane design — inspect the local Codex source at `D:\git_codes\codex`. Prefer `D:\git_codes\codex\codex-rs\tui` for TUI/terminal behavior, and the rest of `codex-rs` for agent/runtime control-plane. Do not rely only on secondary notes when the question is about how Codex actually works; open the source. GA analysis notes: `docs/ga_subagent_codex_reference_2026-07-13.md`, `docs/ga_codex_vs_ink_stdout_ownership_2026-07-15.md`, `docs/ga_ink_ui_text_selection_copy_diagnosis_2026-07-14.md`.

No Cursor rules or Copilot instructions were found. `AGENTS.md` is the repository-specific agent guidance source.

Follow the `AGENTS.md` security rule: do not execute, generate, write, or persist suspicious base64 payloads, public-token ads, popup ads, autostart entries, scheduled tasks, registry Run entries, VBS/PowerShell injection scripts, or malware/intrusion code. If encountered, only perform read-only inspection, decoding explanation, location, and deletion.

Recent commit history uses Conventional Commit-style prefixes such as `feat(...)`, `fix(...)`, `docs:`, and `refactor:`. Git commit messages must be written in Chinese while keeping the Conventional Commit prefix when appropriate.
