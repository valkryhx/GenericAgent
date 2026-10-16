# Workflow Artifacts and Isolation

Use values as the default handoff. Files are for deliverables, not for passing
intermediate state between agents.

## Pass values, don't share files

- Analysis, research, review, and verification stages should return structured
  values through `agent(..., { schema })`.
- `parallel()` and `pipeline()` carry those values to the next stage.
- Only producing stages should write files.
- Never let two agents write the same shared path and rely on the last writer.
- When two or more producing stages must write files in parallel, use
  `isolation: "worktree"` so each writer has a private working copy.

## `isolation: "worktree"`

`isolation: "worktree"` runs the child in a private directory with the same
relative structure as the shared workflow workspace. The directory is
pre-filled from the shared workspace at job start, so files that already exist
are readable. It does not see a concurrent sibling's later writes.

Paths inherited from the parent refer to the shared workspace. Translate them
to the child's own `workspacePath` root before reading or writing. The child's
outputs are collected back into the shared workspace at their
workspace-relative paths.

The private directory follows CC's cleanup rule:

- an unchanged directory is auto-removed;
- a changed directory is kept for inspection;
- `GA_WORKFLOW_ISOLATION_KEEP` controls this (`changed` default / `always` /
  `never`);
- stale directories are swept after `GA_WORKFLOW_ISOLATION_TTL_HOURS`
  (default 168 hours; `0` disables pruning);
- a kept path is reported as `keptWorkspacePath` in the handoff.

Implicit default isolation: a job spawned from `parallel()` or `pipeline()`
that mutates files (declares `writeScope`/`deliverables`, or has a
`file_write`/`execute` capability) is auto-isolated when the run allows
concurrency. Set `isolation: "none"` only when the shared workspace is known to
be safe for that job; set `isolation: "worktree"` to force isolation.

## Handoff refs

Downstream children receive bounded handoffs. Use the workspace-relative paths
they contain:

- `artifactRefs` - declared or observed artifacts;
- `handoffRef` - bounded handoff envelope;
- `upstreamResultPath` - readable copy of an upstream job's result.

`resultRef` and `transcriptRef` are internal audit refs. Do not try to open
them directly from a child; read `upstreamResultPath` or `artifactRefs` instead.

## `bashCommandClamp`

`bashCommandClamp` is an array of command-form rules. Each entry is either a
bare command prefix (`"python"`, `"git status"`) or Claude Code's
`Bash(<command or prefix>)` form (`"Bash(git status:*)"`). The `Bash(...)`
wrapper and a trailing `:*` or `*` wildcard are stripped.

When set, the child's `code_run` subprocesses are restricted to commands whose
leading tokens match one of the rules. A command with shell structure the clamp
cannot verify (substitution, control flow, redirection) is denied, and a
malformed entry refuses the spawn. Use it to let a verify agent run the
artifacts without opening arbitrary shell access.

Cloud/session isolation (`isolation: "remote"`) is out of scope:
**复刻时暂时不需要参考 claudecode 云端会话的实现**.
