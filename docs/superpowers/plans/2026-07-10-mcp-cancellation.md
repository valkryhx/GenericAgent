# MCP Cancellation and Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make MCP connection waits and tool calls stoppable without leaking asyncio tasks or stdio processes.

**Architecture:** A handler-owned cancellation event represents turn lifetime. Manager-owned startup work is shared, while turn-owned tool work is cancelled and drained; FastMCP force-close provides transport and process-tree cleanup.

**Tech Stack:** Python 3.10-3.13, `threading.Event`, `asyncio`, FastMCP 2.14, `unittest`.

---

### Task 1: Turn Cancellation Contract

**Files:**
- Modify: `ga.py`
- Modify: `agentmain.py`
- Modify: `tests/test_mcp_runtime.py`

- [ ] Add a regression test that starts a real stdio MCP connection, calls `GenericAgent.abort()`, and asserts the handler thread exits before the connection timeout.
- [ ] Run the focused test and verify it fails because `ensure_connected()` does not observe the turn cancellation signal.
- [ ] Replace the mutable-list stop flag with an Event-compatible signal, add `GenericAgentHandler.cancel()`, and route `GenericAgent.abort()` through it.
- [ ] Install the handler event around the unchanged two-argument `call_mcp_tool(name, arguments)` invocation.
- [ ] Run the focused test and the existing workflow MCP dispatch compatibility test.

### Task 2: Cancellable Connection Wait

**Files:**
- Modify: `mcp_runtime.py`
- Modify: `tests/test_mcp_runtime.py`

- [ ] Extend the first regression test to assert cancellation is reported separately from server failure.
- [ ] Run it RED against the current synchronous connection bridge.
- [ ] Add a manager-owned connection operation per server and let a turn stop waiting without cancelling or failing the shared startup operation.
- [ ] Ensure manager shutdown cancels and drains unfinished startup operations before closing the event loop.
- [ ] Run the connection cancellation and existing discovery/reconnect tests GREEN.

### Task 3: Tool Cancellation and Timeout Cleanup

**Files:**
- Modify: `mcp_runtime.py`
- Modify: `tests/test_mcp_runtime.py`

- [ ] Add a real hanging-tool regression test that records the stdio PID, cancels through `GenericAgent.abort()`, and asserts the PID exits.
- [ ] Run it RED and capture the existing pending-task or live-process failure.
- [ ] Track the underlying asyncio task rather than cancelling only the bridge Future.
- [ ] On cancellation or timeout, request task cancellation, allow a short grace period, call `Client.close()` when needed, and wait for cleanup.
- [ ] Return cancellation/timeout results without setting the server to `failed`; set it to `pending` only when the connection was closed.
- [ ] Replace the synthetic cancellation-ignoring coroutine test with assertions over real MCP tasks and a clean manager shutdown.

### Task 4: Workflow Child Propagation

**Files:**
- Modify: `workflow_child_agent.py`
- Modify: `tests/test_workflow_child_agent.py`

- [ ] Add a test where a workflow child is blocked in MCP dispatch and `runner.cancel(job)` stops it.
- [ ] Run it RED and verify cancellation currently reaches only the LLM client.
- [ ] Store the active handler in job state and set its cancellation event from `cancel(job)`, including the construction race.
- [ ] Run workflow child and permission inheritance tests GREEN.

### Task 5: Regression Verification

**Files:**
- Review: `ga.py`
- Review: `agentmain.py`
- Review: `mcp_runtime.py`
- Review: `workflow_child_agent.py`

- [ ] Run `python -m unittest tests.test_mcp_runtime -v` and require clean output with no pending-task warning.
- [ ] Run `python -m unittest tests.test_workflow_child_agent -v`.
- [ ] Run `python -m unittest tests.test_agentmain_llm_sessions tests.test_llm_cancel -v`.
- [ ] Run `python -m unittest discover -s tests`.
- [ ] Inspect the final diff for unrelated changes and verify no credential/config files were read or modified.

### Audit Additions Completed

The implementation review found four gaps beyond the original plan and covered each with a RED-to-GREEN regression:

- Workflow cancellation previously resumed `agent_runner_loop`, sent another model request, and published `succeeded`; it now stops consumption and publishes `cancelled`.
- A short-timeout connection waiter previously cancelled the shared startup and inherited the first caller's startup timeout; waiter and manager deadlines are now separate.
- `ensure_all_connected()` previously bypassed the per-server shared Future, allowing concurrent discovery to start duplicate stdio clients and leak stderr handles; all connection entry points now share one Future per server.
- Main-agent and workflow schema discovery previously ran outside the turn cancellation scope and could write an empty cache after stop; discovery is now cancellable and skips incomplete cache writes.

Verification completed on 2026-07-10:

- `python -W always::ResourceWarning -m unittest tests.test_mcp_runtime -v`: 25 passed with no resource or pending-task warnings.
- Agent, LLM, workflow child, scheduler, and runtime focused suites: 91 passed.
- All Git-tracked `tests/test_*.py`: 424 passed, 1 skipped.
- Unrestricted discovery including user-untracked tests: 428 ran with one unrelated error in `test_prompt_guided_planner_real_e2e_contract.py` because it registers 13 agents against `max_total=12`.
