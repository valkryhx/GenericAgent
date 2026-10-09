# GA Workflow Workspace Boundary Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make workflow user artifacts obey one canonical workspace rooted at the GA launch directory, with Codex-style hard path enforcement across planning, child tools, runtime, and verification.

**Architecture:** Resolve one canonical `workspacePath` at bridge startup from `os.getcwd()` (or an explicitly supplied existing directory). Keep the per-run artifact directory for journal/plan/transcript internals only. Normalize every declared artifact to a workspace-relative path before scheduling, and pass the same root plus relative path to every child. Enforce the boundary again inside file tools and runtime checks so prompts cannot bypass it.

**Tech Stack:** Python 3.10+, `pathlib`, existing workflow planner/runtime/store, standard-library `unittest`, Ink bridge real E2E with `tsx`.

---

### Task 1: Add canonical workspace/path security primitives

**Files:**
- Create: `workflow_workspace.py`
- Test: `tests/test_workflow_workspace.py`

- [ ] Write tests for canonical root selection, relative path normalization, Windows drive paths, POSIX absolute paths, `..` traversal, and an in-root absolute path.
- [ ] Run `python -m unittest tests.test_workflow_workspace -v` and confirm the new tests fail because the module does not exist.
- [ ] Implement `resolve_workspace_root`, `normalize_workspace_relative`, `resolve_workspace_child`, and `workspace_metadata`.
- [ ] Make absolute paths outside the root fail closed; allow an absolute path only when it resolves inside the selected root, returning the canonical relative path.
- [ ] Run the focused test file and confirm it passes.

### Task 2: Bind the Ink bridge and workflow runtime to launch cwd

**Files:**
- Modify: `frontends/ink_bridge.py`
- Modify: `workflow_scheduler.py`
- Modify: `workflow_runtime.py`
- Test: `tests/test_ink_bridge.py`
- Test: `tests/test_workflow_runtime.py`

- [ ] Add a bridge workspace root resolved once at construction, defaulting to `Path.cwd()` and accepting an explicit test root.
- [ ] Inject `workspacePath` into workflow plan/run/resume arguments unless the caller supplied the same canonical root.
- [ ] Make runtime choose the explicit workspace first, then persisted workspace, then the launch cwd; never default user artifacts to `run.artifact_dir/workspace`.
- [ ] Persist `workspacePath` and a `workspacePolicy` metadata snapshot on every run.
- [ ] Add regression tests proving a no-args runtime uses the requested launch root and resume preserves it.

### Task 3: Enforce the boundary in child file/code tools

**Files:**
- Modify: `ga.py`
- Modify: `workflow_child_agent.py`
- Test: `tests/test_workflow_child_agent.py` (create if absent)
- Test: `tests/test_ink_bridge.py`

- [ ] Give every workflow child a canonical workspace root in its handler metadata and prompt context.
- [ ] Route `file_read`, `file_write`, and `file_patch` through the shared resolver; reject out-of-root absolute paths and traversal before opening files.
- [ ] Resolve `code_run` cwd through the same boundary and reject a child cwd outside the workflow workspace.
- [ ] Add tests showing `/tmp/x`, `D:\\tmp\\x`, and `../x` cannot write/read outside root, while `tmp/x` succeeds under root.
- [ ] Run the focused child/tool tests.

### Task 4: Normalize planner contracts before validation/scheduling

**Files:**
- Modify: `workflow_planner.py`
- Modify: `workflow_controller.py`
- Modify: `workflow_models.py`
- Test: `tests/test_workflow_plan_validator.py`
- Test: `tests/test_workflow_controller.py`

- [ ] Add a plan normalization pass that rewrites artifact `path`, writer `writeScope`, `deliverables`, acceptance checks, and verifier prompts to one relative path.
- [ ] Reject unresolved absolute/traversal paths during plan creation with a machine-readable `invalid_artifact_path` issue before a run starts.
- [ ] Ensure generated runtime script receives the canonical relative path and never the model's original absolute path.
- [ ] Update planner guidance to describe the workspace contract without relying on it for enforcement.
- [ ] Add tests for `/tmp/report.html` becoming `tmp/report.html` only when the policy can safely interpret it as a conventional workspace-relative prefix; otherwise reject, and for Windows drive/UNC paths always rejecting when outside root.
- [ ] Run planner/controller focused tests.

### Task 5: Make runtime/check adapters consume the same canonical path

**Files:**
- Modify: `workflow_runtime.py`
- Modify: `workflow_check_adapters.py`
- Modify: `workflow_verification.py`
- Test: `tests/test_workflow_runtime.py`
- Test: `tests/test_workflow_check_adapters.py`
- Test: `tests/test_workflow_verification.py`

- [ ] Replace ad-hoc `Path(workspace) / raw` path handling with the shared resolver.
- [ ] Record both `relativePath` and `absolutePath` in artifact evidence while redacting no user content unnecessarily.
- [ ] Keep hard checks for existence/readback/root containment; keep content structure advisory unless explicitly strict.
- [ ] Add regression coverage for the observed `/tmp/liu-guoliang-profile.html` case and for an in-workspace `tmp/liu-guoliang-profile.html` artifact.
- [ ] Run all workflow unit tests.

### Task 6: Update prompts/docs and real validation harness

**Files:**
- Modify: `workflow_planner.py`
- Modify: `workflow_child_agent.py`
- Modify: `frontends/ink-ui/scripts/real_ink_ui_workflow_autonomous_e2e.ts`
- Create: `docs/20261002-ga-workflow-workspace-boundary-implementation.md`

- [ ] Add concise system context: `workspacePath` is canonical, artifact paths are relative, and runtime—not prompt—enforces it.
- [ ] Make the real Ink harness assert the artifact is under `workspacePath` and report both paths.
- [ ] Record implementation decisions, tests, and any remaining limitations.

### Task 7: Verification and real gpt-6-luna Ink tests

- [ ] Run `python -m unittest discover -s tests`.
- [ ] Run Ink UI unit/type checks.
- [ ] Run one real gpt-6-luna standalone subagent task through GA Ink.
- [ ] Run one real gpt-6-luna multi-agent task with MCP and file read/write through GA Ink.
- [ ] Run the Liu Guoliang workflow again and verify the HTML is created below the launch workspace, not `D:\\tmp` or `/tmp`.
- [ ] Save sanitized results in a dated test note; do not record API keys or raw sensitive responses.

