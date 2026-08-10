# Subagent Worktree Isolation Fix Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Avoid worktrees for explicitly read-only subagent tasks and make Git worktree timeouts terminate the complete subprocess tree.

**Architecture:** `ga.py` computes the effective isolation from structured permission metadata before calling `SubagentManager`. `subagent_worktree.py` owns process-tree timeout cleanup. Schemas and the subagent SOP teach the same policy to the model.

**Tech Stack:** Python 3.10-3.13, standard-library `subprocess`, Windows `taskkill`, POSIX process groups, `unittest`.

---

### Task 1: Read-Only Isolation Policy

**Files:**
- Modify: `ga.py`
- Test: `tests/test_ga_subagent_tools.py`

- [ ] Add a failing test where `isolation="worktree"` and an explicit search-only allowlist produce effective isolation `None` with a fallback reason.
- [ ] Add a control test proving `file_patch` capability preserves `worktree`.
- [ ] Run the focused tests and confirm the first test fails for the missing policy.
- [ ] Add a small helper that normalizes tool names and computes requested/effective isolation.
- [ ] Include `requested_isolation` and `isolation_fallback_reason` in the spawn result.
- [ ] Re-run the focused tests and confirm they pass.

### Task 2: Worktree Process-Tree Timeout

**Files:**
- Modify: `subagent_worktree.py`
- Test: `tests/test_subagent_worktree.py`

- [ ] Add a failing subprocess test whose direct child spawns a pipe-holding descendant; assert a short timeout returns promptly.
- [ ] Run the focused test and confirm the old `subprocess.run` path exceeds the deadline.
- [ ] Replace the production `subprocess.run` path with `Popen.communicate(timeout=...)` plus complete process-tree termination.
- [ ] Preserve injected `runner` behavior for existing unit tests.
- [ ] Re-run worktree tests and confirm they pass without leftover probe processes.

### Task 3: Model Guidance

**Files:**
- Modify: `assets/tools_schema.json`
- Modify: `assets/tools_schema_cn.json`
- Modify: `memory/subagent.md`
- Test: `tests/test_ga_subagent_tools.py`

- [ ] Add schema assertions that read-only search must omit worktree isolation.
- [ ] Update both schema descriptions and the SOP with the approved policy.
- [ ] Run schema tests and confirm they pass.

### Task 4: Verification And Cleanup

**Files:**
- Test: `tests/test_ga_subagent_tools.py`
- Test: `tests/test_subagent_worktree.py`
- Test: `tests/test_subagent_manager.py`

- [ ] Run focused subagent and worktree tests.
- [ ] Run `python -m compileall` for changed Python files.
- [ ] Run `git diff --check`.
- [ ] Run `python -m unittest discover -s tests`.
- [ ] Verify and terminate only the orphan Git process from `run_000089`, then confirm no matching process remains.
