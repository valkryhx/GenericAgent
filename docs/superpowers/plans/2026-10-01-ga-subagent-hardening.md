# GA Subagent Hardening Implementation Plan

> REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Replace prompt-only subagent discipline with Pi-style hard isolation, explicit capabilities, and auditable lifecycle for subagent, multi-agent, agent team, and workflow.

**Architecture:** Keep the existing file IPC, event bus, artifact store, and `spawn_agent` API. Change the default child to an isolated context without orchestration tools. Transfer parent context only through explicit `fork_turns`, messages, artifacts, or workflow chain values.

---

## Task 1: Default isolated context

Files: `ga.py`, `subagent_manager.py`, `tests/test_ga_subagent_permissions.py`, `tests/test_subagent_manager.py`.

- [ ] Add a failing test proving that omitted `fork_turns` sends `fork_turns="none"` and `fork_history=None`; explicit `all` still preserves history.
- [ ] Run `python -m unittest tests.test_ga_subagent_permissions tests.test_subagent_manager -v` and observe the new test fail for the current `all` default.
- [ ] Change the default in `ga.py` to `none` and record `context_mode=isolated` or `explicit_fork` in spawn metadata.
- [ ] Rerun the focused tests and commit `fix(subagent): default children to isolated context`.

## Task 2: Hard capability profile and recursion boundary

Files: create `subagent_capabilities.py`; modify `agentmain.py`, `ga.py`, `subagent_permissions.py`; test `tests/test_subagent_capabilities.py`.

- [ ] Add failing tests proving that a child profile removes `spawn_agent`, `list_agents`, `wait_agent`, `read_agent_result`, `resume_agent`, `send_message`, `followup_task`, `foreground_agent`, `background_agent`, `attach_agent`, `detach_agent`, `interrupt_agent`, and `close_agent`, while the root profile keeps them.
- [ ] Implement `SubagentCapabilityProfile`, `ORCHESTRATION_TOOLS`, `INTERNAL_SENTINEL_TOOLS={"no_tool"}`, and `build_subagent_capability_profile()`.
- [ ] Filter the schema in `agentmain.load_tool_schema` after construction. Keep orchestration tools only when `allow_delegation=true` is explicitly present.
- [ ] Make `SubagentPermissionPolicy` evaluate `no_tool` first, capability denies second, and business allowlists third.
- [ ] Run `python -m unittest tests.test_subagent_capabilities tests.test_ga_subagent_permissions tests.test_agentmain_model_selection -v`.
- [ ] Commit `feat(subagent): enforce child capability boundaries`.

## Task 3: Make role metadata authoritative

Files: `subagent_roles.py`, `subagent_manager.py`, `ga.py`, `assets/tools_schema.json`, `tests/test_subagent_roles.py`, `tests/test_subagent_manager.py`.

- [ ] Add failing tests proving that a role can declare tools, model, and `allow_delegation`; spawn writes these to state; unknown tools are rejected; legacy roles without tools still lose orchestration tools.
- [ ] Normalize role capabilities and write `capability_profile`, `allowed_tools`, `denied_tools`, `context_mode`, and `allow_delegation` to state and spawn response.
- [ ] Update `spawn_agent` description to state isolated context, no recursion, and explicit `fork_turns` rules.
- [ ] Run role and manager regressions and commit `feat(subagent): make role capabilities explicit`.

## Task 4: Separate completion from process lifecycle

Files: `agentmain.py`, `subagent_manager.py`, `subagent_state.py`, `tests/test_subagent_manager.py`, `tests/test_subagent_event_bus.py`.

- [ ] Add failing tests proving that close does not delete persisted final output, `completed + waiting_reply` is not reported as running, failures record `last_error_stage`, closed/exited children remain readable, and abort does not clear an existing final output.
- [ ] Implement `completion_status`, `process_status`, `final_output_ref`, and `last_error_stage`; close changes process status and close reason without overwriting completion or artifacts.
- [ ] Centralize `record_agent_failure(stage, error)` and make `read_agent_result` prefer artifact, then output file, then state summary. Empty output must be an explicit `empty_output` failure.
- [ ] Run manager and event-bus regressions and commit `fix(subagent): separate completion from process lifecycle`.

## Task 5: Real acceptance

Files: `tests/real_subagent_realtime_e2e.py`, `tests/real_workflow_forward_matrix_e2e.py`, `docs/20261001-ga-subagent-hardening-e2e.md`.

- [ ] Run `python -m unittest discover -s tests`.
- [ ] Serially run two independent child scenarios and one explicit chain scenario with real `deepseek-v4.1-flash`. Record process_entry to turn_started, turn_started to turn_completed, tool calls, artifact, and close state.
- [ ] Run workflow, agent-team, and multi-agent scenarios. Verify planner/runtime/synthesis use explicit messages or artifacts rather than implicit full history.
- [ ] Write the acceptance report. Every conclusion must be cross-checked against state, events, and artifacts.

## Acceptance metrics

- Default child uses `fork_turns=none` and has no `_history.json`.
- Child schema has no orchestration tools unless `allow_delegation=true` is explicit.
- `no_tool` never produces `subagent_tool_not_allowed`.
- Existing final output and artifacts remain readable after process close.
- Real DeepSeek subagent, multi-agent, agent-team, and workflow scenarios have auditable state evidence.
- The full test suite has no new failures.
