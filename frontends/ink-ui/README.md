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
ga ink
```

or:

```powershell
npm --prefix frontends/ink-ui start --
```

## Current Scope

- Single active chat session.
- Streaming assistant updates from the Python bridge.
- Multiline paste folding with `[Copied text #N +M lines]`.
- Enter sends, `Ctrl+J` inserts a newline, `Ctrl+C` exits.

Session sidebar, historical restore, Markdown final rendering, and full virtual
scrolling are intentionally left for later iterations.
