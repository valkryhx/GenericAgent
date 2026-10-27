# Workflow API Reference

## 2. Tool inputs

- `script` — the inline workflow script. Prefer this for a new workflow. Do not
  write the script to a file first.
- `scriptPath` — a previously saved script inside the workflow workspace. Use
  this to iterate without resending the full script.
- `name` — run a workflow saved in the named-workflow library.
- `args` — JSON value injected into the script as the global `args`.
- `resumeFromRunId` — resume from an earlier run. The longest unchanged prefix of
  `agent()` calls is replayed from cache.
- `timeoutSeconds` — optional run timeout; the product default is 900 seconds.

Exactly one source is required: `script`, `scriptPath`, `name`, or
`resumeFromRunId` (a resume may be combined with `scriptPath`).

The tool returns immediately with `status: "async_launched"` and a `runId`. The
workflow continues in the background. When it finishes, GA queues a
`<workflow_handoff>` message for the main agent. Use `/workflows` to watch live

**Do not block on the run.** After `workflow` returns `async_launched`, report
the launch in one line and **end your turn**. Do not call `wait_agent` (or poll
`/workflows`) to wait for the run to finish. GA uses Claude Code's background-task
model (`m2000`: "Running in background · /workflows to monitor and save"): the
session stays free for the user while the run proceeds, and a `<workflow_handoff>`
message wakes you automatically when it completes. Blocking the turn with
`wait_agent` locks the Ink composer for the whole run.
progress, inspect a run, stop it, save it, or run a saved workflow again.

## 3. Script contract

Every script must begin with exactly this shape:

```js
export const meta = {
  name: 'review-changes',
  description: 'Review changed files across dimensions and verify each finding',
  phases: [
    { title: 'Review' },
    { title: 'Verify' },
  ],
}
```

Rules:

- `meta` must be the **first statement**.
- `meta` must be a **pure literal**: no variables, calls, spreads, computed
  keys, methods, accessors, or template interpolation.
- Required fields: `name` and `description`, both non-empty strings.
- Optional fields: `whenToUse` (string) and `phases` (array of
  `{ title, detail?, model? }`; GA accepts `model` in meta but the current child
  runner does not use it).
- Use the same phase titles in `meta.phases` and in `phase(...)` calls.
- The script is plain JavaScript, not TypeScript.
- Maximum script size is 524288 bytes.
- The script must be deterministic. `Date.now()`, `Math.random()`, and
  no-argument `new Date()` are unavailable because they break resume. Pass
  timestamps through `args`, or stamp results after the workflow returns.
- `return` the final value from the script. Return JSON-serializable data.

## 4. DSL

### `agent(prompt, options?)`

Spawns one child agent. Without `options.schema` it returns the child's final
text as a string. With `options.schema` it returns the validated structured
object.

GA-supported options:

- `label` — display label. Use a stable, descriptive label.
- `phase` — progress group. Prefer this over the global `phase()` call inside
  `parallel()` or `pipeline()` stages.
- `schema` — JSON Schema for structured output (object-rooted). The child is
  given a `structured_output` tool whose input schema is this schema and must
  call it once; the call is validated at the tool-call layer (the model retries
  on mismatch) and `agent()` returns that object. It is NOT a request to emit
  JSON as free text, so do not write prompts that ask the child to "return JSON".
  This is the primary Claude Code path for structured results. GA's
  `executionContract` / `requiredToolEvidence` remains an optional host
  extension; do not require it unless the task explicitly asks for that gate.
  Evidence for an ordinary workflow is the child's actual `toolSummary` or
  transcript plus the observed artifact, not a prose self-report.
- `fallback` — `"text"` to degrade to text when schema validation fails.
- `role` — one of `understanding`, `research`, `contract`, `tests`,
  `implementation`, `verification`, `review`, `repair`, `summary`, `synthesis`.
- `toolProfile` — planning hint only: `planner`, `research`, `authoring`, `verify`, or `*`. Workflow children receive the full connected tool schema; the host does not pre-deny tools from this value.
- `pathPolicy` — `"unrestricted"` (default) or `"workspace"`. `workspacePath` is always the child cwd and artifact root. The default allows reads/writes/commands outside it; set `"workspace"` to opt into the hard workspace path ACL.
- `writeScope` — array of workspace-relative paths this agent may write.
- `deliverables` — array of expected output paths or names.
- `acceptanceChecks` — array of machine-checkable acceptance ids.
- `capabilities` — array from `web_search`, `web_fetch`, `file_read`,
  `file_write`, `execute`.
- `requiredTools` — array of concrete GA tool names the agent should use.
- `dependsOn` — array of upstream labels.
- `maxTurns` — maximum LLM/tool steps for this one child execution. Integer `1..80`; default `80`. It does not increase `retryPolicy.maxAttempts`.
- `retryPolicy` — `{ maxAttempts: 1..3, backoffMs, retryableErrors }`. A `max_turns_exceeded` retry raises the single-attempt budget by 40 steps (capped at 80) and still stops at `maxAttempts`.
- `isolation` — `"worktree"` or `"none"`. `"worktree"` runs the child in its own
  private directory instead of the shared workspace, so parallel writers cannot
  collide. Like CC's git worktree, that directory is a **working copy with the
  same relative file structure**: it is pre-filled with the shared workspace's
  contents at job start (so the child can still read files that already exist),
  but it does not see a concurrent sibling's later writes. Paths inherited from
  the parent refer to the shared workspace -- translate them to the child's own
  root. The child's outputs are collected back into the shared workspace at
  their workspace-relative paths. EXPENSIVE (extra disk + setup per agent): use
  it ONLY when two or more agents write files in parallel and would otherwise
  overwrite each other. Prefer passing values (via `schema`) over writing files.
  **Implicit default isolation:** you usually do NOT need to write this. A job
  spawned from `parallel()`/`pipeline()` that mutates files (declares
  `writeScope`/`deliverables`, or a `file_write`/`execute` capability) is
  auto-isolated when the run allows concurrency, mirroring Claude Code's
  enforced isolation for parallel sessions. Set `isolation: "none"` to opt out
  when you know the shared workspace is safe for that job (CC's
  `bgIsolation: "none"`); set `isolation: "worktree"` to force it.
  The private directory follows CC's cleanup rule -- it is auto-removed if
  unchanged, and a changed one is kept for inspection. Tune with
  `GA_WORKFLOW_ISOLATION_KEEP` (`changed` default / `always` / `never`); stale
  directories are swept after `GA_WORKFLOW_ISOLATION_TTL_HOURS` (default 168h,
  `0` disables). A kept path is reported as `keptWorkspacePath` in the handoff.
- `bashCommandClamp` — array of command-form rules. Each entry is either a bare
  command prefix (`"python"`, `"git status"`) or Claude Code's
  `Bash(<command or prefix>)` form (`"Bash(git status:*)"`; the `Bash(...)`
  wrapper and a trailing `:*` / `*` wildcard are stripped). When set, the child's
  `code_run` subprocesses are restricted to commands whose leading tokens match
  one of the rules; a command with shell structure the clamp cannot verify
  (substitution, control flow, redirection) is denied, and a malformed entry
  refuses the spawn. Use it to let a verify agent actually run the artifacts
  without opening up arbitrary shell.

Use GA tool names in prompts, not CC names: `file_read`, `file_write`,
`file_patch`, `code_run`, `web_scan`, `web_execute_js`, `load_skill`,
`spawn_agent`, and the other names in GA's tool schema.

### `parallel(items)`

`items` is an array of **functions**. Each function is called to start one
branch. Returns results in input order. A branch that throws or rejects becomes
`null`; the other branches continue. Wrap agent calls:

```js
const results = await parallel([
  () => agent('Review security', { label: 'security', phase: 'Review' }),
  () => agent('Review performance', { label: 'perf', phase: 'Review' }),
])
```

### `pipeline(items, ...stages)`

Runs each item independently through all stages. There is no barrier between
stages: item A can reach stage 2 while item B is still in stage 1. A `null`
value short-circuits that item's remaining stages. A stage that throws makes
that item `null` without stopping other items.

**Default to `pipeline()`.** Only reach for a barrier (two `parallel()` calls
around a plain-code transform) when stage N genuinely needs *all* of stage N-1
together — e.g. dedup/merge across the full result set, an early exit when the
total is zero, or a stage whose prompt references "the other findings". A barrier
is **not** justified by "I want to flatten/map/filter first" (do that inside a
stage), "the stages are conceptually separate", or "it's cleaner code" — barrier
latency is real. Smell test: if you wrote `await parallel(...)` → pure
`transform(...)` → `await parallel(...)`, that middle transform does not need the
barrier; rewrite it as one pipeline with the transform inside a stage.

Canonical multi-stage pattern (the `review-changes` shape): pipeline by default,
each dimension's findings verify as soon as that dimension finishes — no wasted
wall-clock. `FINDINGS_SCHEMA` and `VERDICT_SCHEMA` are JSON Schemas you define;
the verdict must carry an `isReal` field.

```js
const DIMENSIONS = [{ key: 'bugs', prompt: '...' }, { key: 'perf', prompt: '...' }]
const results = await pipeline(
  DIMENSIONS,
  d => agent(d.prompt, { label: `review:${d.key}`, phase: 'Review', schema: FINDINGS_SCHEMA }),
  review => parallel(review.findings.map(f => () =>
    agent(`Adversarially verify: ${f.title}`, { label: `verify:${f.file}`, phase: 'Verify', schema: VERDICT_SCHEMA })
      .then(v => ({ ...f, verdict: v }))
  )),
)
const confirmed = results.flat().filter(Boolean).filter(f => f.verdict?.isReal)
return { confirmed }
```

### `phase(name)` and `log(message)`

`phase(name)` starts a progress group. `log(message)` records a workflow log
line. Inside concurrent stages, prefer `agent(..., { phase: '...' })` because
the global `phase()` call races between concurrent branches.

### `args`

The JSON value passed to the tool is available as the global `args`. Use it for
timestamps, user constraints, input paths, and other run-specific data.

### `workflow(nameOrRef, args)`

Runs another workflow from inside this one. **GA supports exactly one level of
nesting**: a workflow started this way cannot itself call `workflow()`, and a
second-level call fails with `nested workflows are limited to one level`.

`nameOrRef` is one of:

- a string — the name of a saved workflow in the named-workflow library;
- `{ name }` — same as the string form;
- `{ script }` — an inline script;
- `{ scriptPath }` — a script saved in the workflow workspace.

`args` is injected into the nested script as its global `args`. The call resolves
to the nested workflow's final return value. The nested run is recorded as its
own run (with `parentRunId` set) and appears in `/workflows`; stopping the outer
run also stops the nested run.

```js
const inner = await workflow('lint-package', { packageDir })
// one-off nested script:
const inner2 = await workflow({ script: nestedScript }, { input })
```

### `runPythonUnittest(workspaceOrSpec, options?)`

GA-specific host gate. Runs Python `unittest` in a workspace and returns a
structured gate result. The workspace must be inside the workflow workspace.

```js
const gate = await runPythonUnittest(workspacePath, {
  startDir: '.',
  pattern: 'test_*.py',
  timeoutMs: 30000,
})
if (!gate.gatePassed) {
  return { ok: false, gate }
}
```

### `repairAndRetest(spec)`

GA-specific bounded repair loop. Runs the test gate, then asks a repair agent to
read `TEST_FAILURES.txt` and fix the workspace, up to `maxAttempts`.

```js
const repaired = await repairAndRetest({
  workspacePath,
  maxAttempts: 2,
  repairPrompt: 'Fix only the failing behavior and keep the change minimal.',
})
```
