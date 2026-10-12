---
name: workflow-authoring
description: Author GenericAgent dynamic workflow JavaScript scripts (meta contract, agent/parallel/pipeline/phase/log, schemas, artifacts, test gates, resume). Load this before calling the `workflow` tool.
when_to_use: Before writing or editing a dynamic workflow script, or when the user explicitly asks to run a multi-agent workflow.
---

# GenericAgent Dynamic Workflow Authoring

This skill is the GA-localized version of Claude Code 2.1.283's
`workflow-authoring-reference.md`. It describes **only the APIs GA actually
supports**. Do not invent CC-only fields such as `budget`, `model`, `effort`,
`isolation: "remote"`, `agentType`, `disallowedTools`, or `bashCommandClamp`.

## 1. When to call the `workflow` tool

Call `workflow` only when the user has explicitly opted into multi-agent
orchestration. Explicit opt-in means one of:

- the user said "use a workflow", "run a workflow", "fan out agents",
  "orchestrate this with subagents", or equivalent words in their own request;
- the user invoked a skill or slash command whose instructions say to run a
  workflow;
- the user asked to run a specific named or saved workflow.

A task that would merely benefit from a workflow does **not** count. If the user
did not opt in, either use `spawn_agent` for one bounded side task or briefly
explain what a workflow could do and ask whether to run it.

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
- `schema` — JSON Schema for structured output.
- `fallback` — `"text"` to degrade to text when schema validation fails.
- `role` — one of `understanding`, `research`, `contract`, `tests`,
  `implementation`, `verification`, `review`, `repair`, `summary`, `synthesis`.
- `toolProfile` — `planner`, `research`, `authoring`, `verify`, or `*`.
- `writeScope` — array of workspace-relative paths this agent may write.
- `deliverables` — array of expected output paths or names.
- `acceptanceChecks` — array of machine-checkable acceptance ids.
- `capabilities` — array from `web_search`, `web_fetch`, `file_read`,
  `file_write`, `execute`.
- `requiredTools` — array of concrete GA tool names the agent should use.
- `dependsOn` — array of upstream labels.
- `retryPolicy` — `{ maxAttempts: 1..3, backoffMs, retryableErrors }`.

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

```js
const results = await pipeline(
  dimensions,
  d => agent(d.prompt, { label: `review:${d.key}`, phase: 'Review' }),
  review => parallel(review.findings.map(f => () =>
    agent(`Verify: ${f.title}`, { label: `verify:${f.key}`, phase: 'Verify' })
  )),
)
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

## 5. Patterns that improve complex-task quality

- **Adversarial verify** — spawn independent skeptics for each important
  finding, prompt them to refute it, and keep it only when enough skeptics fail
  to refute.
- **Perspective-diverse verify** — give each verifier a distinct lens
  (correctness, security, performance, reproducibility) instead of N identical
  copies.
- **Judge panel** — generate independent approaches, score them with separate
  judges, and synthesize from the winner while grafting the best ideas from the
  runners-up.
- **Loop until dry** — for unknown-size discovery, keep searching until several
  consecutive rounds return no new findings. Add a hard iteration cap.
- **Multi-modal sweep** — use different search angles in parallel so one angle's
  blind spot is covered by another.
- **Completeness critic** — end with an agent that asks what modality, claim, or
  source is still missing.
- **No silent caps** — if the workflow samples, truncates, or skips work, call
  `log()` and include that fact in the final result.

Scale the fan-out to what the user asked for. A quick review gets a small number
of finders and one verify pass. A comprehensive audit gets a larger finder pool,
multi-vote verification, and a synthesis stage.

## 6. Resume

The tool result includes `runId`. To resume after a pause, kill, or script edit,
call `workflow` again with `resumeFromRunId` and, when iterating on a saved
script, `scriptPath`. The longest unchanged prefix of `agent()` calls returns
cached results; the first changed or new call and everything after it runs live.

Before diagnosing an empty or unexpected result, read the run's journal and
agent result files under the run transcript directory. Do not assume a cached
result is non-empty.

## 7. Provenance and boundaries

Upstream reference: `D:\git_codes\claudecode283_hack\claudecode283\extracted\workflow-authoring-reference.md`
(Claude Code 2.1.283). This file localizes the parts that GA implements. GA
supports CC's one-level `workflow()` nested API (a nested workflow cannot itself
call `workflow()`). GA does not implement `budget`, `isolation`, `model`,
`effort`, `agentType`, `disallowedTools`, or `bashCommandClamp`. GA's
`runPythonUnittest` and `repairAndRetest` are GA-specific additions.
Cloud/session isolation (`isolation: "remote"`) is out of scope:
**复刻时暂时不需要参考 claudecode 云端会话的实现**.
