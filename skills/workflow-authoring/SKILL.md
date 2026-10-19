---
name: workflow-authoring
description: Author GenericAgent dynamic workflow JavaScript scripts (meta contract, agent/parallel/pipeline/phase/log, schemas, artifacts, test gates, resume). Load this before calling the `workflow` tool.
when_to_use: Before writing or editing a dynamic workflow script, or when the user explicitly asks to run a multi-agent workflow.
---

# GenericAgent Dynamic Workflow Authoring

Thin L2 entry for GA's Claude Code 2.1.283-compatible workflow contract.
It describes only APIs GA supports. Details live in `references/`; the loader
prepends `Base directory for this skill: ...`. Use `file_read` with an absolute
path under that base directory when needed. Do not load every reference up front.

## Read the right reference

| Need | Read |
| --- | --- |
| API, options, tool inputs | `references/api.md` |
| Patterns and examples | `references/examples.md` |
| Resume and cached prefixes | `references/resume.md` |
| Artifacts, isolation, handoffs, clamps | `references/artifacts.md` |
| Test gates | `references/testing.md` |

## When to call `workflow`

Call `workflow` only when the user explicitly opted in: "use a workflow",
"run a workflow", "fan out agents", "orchestrate this with subagents", a
workflow skill/slash command, or a named/saved workflow. A task that would
merely benefit from a workflow does not count. Without opt-in, use
`spawn_agent` for one bounded side task or explain the workflow option first.

GA follows CC's direct-JS entry: the main agent is the script author. There is
no JSON planning step and no separate planner. Load this skill, write the
JavaScript inline, and call `workflow` with `script`.

After `workflow` returns `status: "async_launched"`, report the launch in one
line and end the turn. Do not call `wait_agent` or poll `/workflows`; the run is
background work and a `<workflow_handoff>` message wakes you when it settles.
Use `/workflows` to watch, inspect, stop, save, or re-run a run.

## Minimal script contract

Every script must begin with a pure-literal `meta` as its first statement:

```js
export const meta = {
  name: 'review-changes',
  description: 'Review changed files across dimensions and verify each finding',
  phases: [{ title: 'Review' }, { title: 'Verify' }],
}
```

- Required: non-empty `name` and `description`.
- Optional: `whenToUse` and `phases` (`{ title, detail?, model? }`).
- Use the same phase titles in `meta.phases` and `phase(...)` calls.
- Plain JavaScript, not TypeScript. Maximum script size: 524288 bytes.
- Deterministic: `Date.now()`, `Math.random()`, and no-argument `new Date()`
  are unavailable because they break resume. Pass timestamps through `args`.
- `return` the final JSON-serializable value.

## Minimal API

- `agent(prompt, options?)` starts one child. Without `options.schema` it
  returns final text; with `options.schema` it returns a validated object. Do
  not ask a schema child to "return JSON" in prose; the host supplies the
  `structured_output` tool and validates it at the tool-call layer.
- `schema` is CC's primary structured-output path. GA's
  `executionContract` / `requiredToolEvidence` is an optional host acceptance
  extension, not a requirement for every workflow. Without an explicit
  contract, acceptance must come from actual child tool evidence
  (`toolSummary` / transcript) and real artifacts, never from prose claiming
  success.
- `parallel(items)` takes an array of functions and returns results in input
  order. A failing branch becomes `null`; other branches continue.
- `pipeline(items, ...stages)` runs each item through all stages independently,
  with no barrier between stages. A `null` value short-circuits that item.
  Default to `pipeline()`; use a barrier only when stage N genuinely needs all
  of stage N-1 together.
- `phase(name)` starts a progress group; `log(message)` records a workflow log.
  Prefer `agent(..., { phase: '...' })` inside concurrent branches.
- `args` is the JSON value passed to the `workflow` tool.
- `workflow(nameOrRef, args)` runs one nested workflow. GA supports exactly one
  level; the nested workflow cannot call `workflow()` again.
- `runPythonUnittest(...)` and `repairAndRetest(...)` are GA-specific host test
  gates. Read `references/testing.md` before using them.

Full options: `label`, `phase`, `schema`, `fallback`, `role`, `toolProfile`,
`writeScope`, `deliverables`, `acceptanceChecks`, `capabilities`,
`requiredTools`, `dependsOn`, `retryPolicy`, `isolation`, and
`bashCommandClamp`. See `references/api.md`.

## Common pitfalls

- Do not invent CC-only fields such as `budget`, `model`, `effort`, `agentType`,
  or `disallowedTools`; GA does not implement them.
- Do not use `isolation: "remote"`. Cloud/session isolation is out of scope:
  **复刻时暂时不需要参考 claudecode 云端会话的实现**.
- Pass values, don't share files. Use `schema` and let `parallel()` or
  `pipeline()` carry results forward. Only producing stages write files.
- For parallel writers, use `isolation: "worktree"` or let GA's implicit
  isolation apply. Never let two agents write the same shared path and hope the
  last write wins.
- Use GA tool names in prompts: `file_read`, `file_write`, `file_patch`,
  `code_run`, `web_scan`, `web_execute_js`, `load_skill`, `spawn_agent`, etc.
- If a workflow samples, truncates, or skips work, call `log()` and include that
  fact in the final result.
