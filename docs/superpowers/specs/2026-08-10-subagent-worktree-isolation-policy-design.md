# Subagent Worktree Isolation Policy Design

## Goal

Prevent read-only research subagents from paying the cost and failure risk of Git worktree creation, while keeping worktree isolation available for agents that can modify the repository.

## Policy

- `isolation` remains opt-in and defaults to no worktree.
- A requested `worktree` is downgraded when the effective permission boundary cannot use local workspace mutation tools.
- Read-only permission profiles and explicit static allowlists without `file_write`, `file_patch`, or `code_run` qualify for downgrade.
- The spawn result reports the requested isolation and the downgrade reason so the model can see what happened.
- Coding agents with local mutation capability keep `worktree` unchanged.

## Runtime Safety

Worktree creation still needs a hard timeout. On timeout, GA must terminate the entire subprocess tree before collecting stdout/stderr. Killing only the Git-for-Windows wrapper leaves the inner `mingw64/bin/git.exe` alive with inherited pipes and can block `communicate()` indefinitely.

## Documentation

The English and Chinese tool schemas and `memory/subagent.md` must state that worktree isolation is for repository modification, not read-only search and not security sandboxing.

## Verification

- Unit tests cover read-only downgrade and mutating-task preservation.
- A subprocess regression test reproduces a child that leaves a pipe-holding descendant behind.
- Existing subagent manager, GA tool, worktree, and full unittest suites remain green.
