# GenericAgent Ink UI

Experimental React/Ink terminal frontend for GenericAgent.

This frontend is intentionally parallel to `frontends/tuiapp_v2.py`. It talks to
the Python runtime through `frontends/ink_bridge.py` over JSONL stdio and does
not import or modify the Textual UI.

## Run

Install dependencies once:

```powershell
npm install --prefix frontends/ink-ui
```

Start from the repository root:

```powershell
ga
```

or explicitly:

```powershell
ga ink
```

## Current Scope

- Single active chat session.
- Streaming assistant updates from the Python bridge.
- Multiline paste folding with `[Copied text #N +M lines]`.
- Enter sends when idle; while running, drafts stay in the input box.
- `/stop` or `Esc` stops the backend task; `Ctrl+J` inserts a newline; `Ctrl+C` exits.
- `/resume` / `/continue` opens a Claude-style session picker; `/resume N` restores by index.
- `/rewind` / `/checkpoint` opens a user-message picker and rewinds conversation state.
- Long assistant output is tail-capped in the visible transcript.
- Verbose tool argument blocks are folded in the UI.

Markdown final rendering, file snapshots for code rewind, and full virtual
scrolling are intentionally left for later iterations.
