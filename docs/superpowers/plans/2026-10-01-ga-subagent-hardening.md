# GA Subagent Hardening Implementation Plan

> REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace prompt-only subagent discipline with Pi-style hard isolation, explicit capabilities, and auditable lifecycle for subagent, multi-agent, agent team, and workflow.

**Architecture:** Keep the existing file IPC, event bus, artifact store, and `spawn_agent` API. Change the default child to an isolated context without orchestration tools. Transfer parent context only through explicit `fork_turns`, messages, artifacts, or workflow chain values.

---

## Task 1: Default isolated context

Files: `ga.py`, `subagent_manager.py`, `tests/test_ga_subagent_permissions.py`, `tests/test_subagent_manager.py`.

- [x] Add a failing test proving that omitted `fork_turns` sends `fork_turns="none"` and `fork_history=None`; explicit `all` still preserves history.
- [x] Run `python -m unittest tests.test_ga_subagent_permissions tests.test_subagent_manager -v` and observe the new test fail for the current `all` default.
- [x] Change the default in `ga.py` to `none` and record `context_mode=isolated` or `explicit_fork` in spawn metadata.
- [x] Rerun the focused tests.

## Task 2: Hard capability profile and recursion boundary

Files: create `subagent_capabilities.py`; modify `agentmain.py`, `ga.py`, `subagent_permissions.py`; test `tests/test_subagent_capabilities.py`.

- [x] Add failing tests proving that a child profile removes orchestration tools while the root profile keeps them.
- [x] Implement `SubagentCapabilityProfile`, `ORCHESTRATION_TOOLS`, `INTERNAL_SENTINEL_TOOLS={"no_tool"}`, and `build_subagent_capability_profile()`.
- [x] Filter the per-turn schema after construction and apply a runtime dispatch deny as a second boundary.
- [x] Preserve `no_tool` as an engine sentinel before business allowlists.
- [x] Run capability, permission, manager, role, and tool regressions.

## Task 3: Make role metadata authoritative

Files: `subagent_roles.py`, `subagent_manager.py`, `ga.py`, `assets/tools_schema.json`, `tests/test_subagent_roles.py`, `tests/test_subagent_manager.py`.

- [x] Add tests proving that a role can declare tools and `allow_delegation`; spawn writes capability metadata to state and child command.
- [x] Normalize role capabilities and write `capability_options`, `capability_profile`, `context_mode`, and `allow_delegation` to state and spawn response.
- [x] Update `spawn_agent` description to state isolated context and no default recursion.
- [x] Run role and manager regressions.

## Task 4: Separate completion from process lifecycle

Files: `agentmain.py`, `subagent_manager.py`, `subagent_state.py`, `tests/test_subagent_manager.py`, `tests/test_subagent_event_bus.py`.

- [x] Add a regression proving that an empty final output is not reported as success.
- [x] Add `completion_status` and `last_error_stage` to observed child state; close preserves persisted final output and completion state.
- [x] Mark startup-handshake and turn failures with explicit stages; empty output is `empty_output`.
- [x] Run manager and event-bus regressions.

## Task 5: Real acceptance

Files: `tests/real_subagent_realtime_e2e.py`, `tests/real_workflow_forward_matrix_e2e.py`, `docs/20261001-ga-subagent-hardening-e2e.md`.

- [x] Run `python -m unittest discover -s tests`.
- [x] Serially run real `deepseek-v4.1-flash` child/control-plane scenarios and record state, process, artifact, and replay evidence.
- [x] Run the real forward workflow matrix, including direct, workflow, delegated, fallback, approval, and eval-contract scenarios.
- [x] Write the acceptance report. Conclusions are cross-checked against state, events, and artifacts.

## Acceptance metrics

- Default child uses `fork_turns=none` and has no `_history.json`.
- Child schema has no orchestration tools unless `allow_delegation=true` is explicit.
- `no_tool` never produces `subagent_tool_not_allowed`.
- Existing final output and artifacts remain readable after process close.
- Real DeepSeek subagent, multi-agent, agent-team, and workflow scenarios have auditable state evidence.
- The full test suite has no new failures.
