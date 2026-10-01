# Pi Subagent Design Reference and GA Comparison

- Date: 2026-10-01
- Source: `D:\git_codes\pi\packages\coding-agent\examples\extensions\subagent`
- Durable examples: `D:\git_codes\pi\packages\durable	est\examples-subagent-foreground.ts` and `23-subagent-background.ts`
- Purpose: translate Pi's hard stability mechanisms into GenericAgent constraints for subagent, multi-agent, agent team, and dynamic workflow.

## 1. Pi mechanisms

### 1.1 Independent context

Pi starts every child as a separate process with `--mode json -p --no-session`. The task is passed explicitly and the role prompt is appended from a temporary file. The child does not inherit parent conversation history, tool calls, or orchestration instructions.

This prevents two failure classes: the child mistaking the parent's task for its own task, and the child recursively calling wait/spawn tools after seeing the parent's transcript.

### 1.2 Declarative role capabilities

Pi agents are markdown files with frontmatter such as:

```markdown
---
name: scout
description: Fast codebase recon
tools: read, grep, find, ls, bash
model: claude-haiku-4-5
---
Role instructions and an output contract
```

The frontmatter becomes runtime configuration: `model` selects the model, `tools` selects the tool set, and the body becomes an appended system prompt. The prompt guides behavior, but the process and registry enforce the boundary.

### 1.3 No recursive subagent by default

The durable foreground implementation removes the `subagent` tool from the child active tool set. This is a physical capability boundary, not a request for the model to behave. A child therefore cannot create a second orchestration tree unless the caller explicitly opts into that capability.

### 1.4 Explicit scheduling modes

Pi separates `single`, `parallel`, and `chain` modes. Parallel uses a bounded worker pool. Chain passes output through an explicit `{previous}` placeholder. No hidden fork history is used to transfer context.

### 1.5 Structured lifecycle and bounded output

Each child returns structured exit code, stop reason, stderr, messages, usage, model, and final output. Parallel model-visible output is capped while the full result remains in tool details. Parent abort propagates to the child process, and failures are returned as diagnostics rather than empty success.

## 2. GA evidence from 2026-10-01

### 2.1 Default fork history caused identity confusion

GA's `ga.py` used `fork_turns=all` when the argument was omitted. The parent backend history was written to `_history.json`. In a real `deepseek-v4.1-flash` run, both children had `fork_turns=all` and `fork_history_count=2`. They saw the parent's instruction to spawn and wait for two children, then repeated wait/read behavior instead of executing their own MCP task.

An explicit `fork_turns=none` run entered the child turn in about one second and executed the assigned task.

### 2.2 Children still had orchestration tools

GA reused the complete tools schema for children, including `spawn_agent`, `list_agents`, `wait_agent`, `read_agent_result`, `resume_agent`, `send_message`, `followup_task`, `foreground_agent`, `background_agent`, `attach_agent`, `detach_agent`, `interrupt_agent`, and `close_agent`. Prompt hints were not enough to prevent recursive or confused scheduling.

### 2.3 Internal sentinel was treated as a business tool

The agent loop uses `no_tool` to represent a direct model answer. The allowlist check rejected it as `subagent_tool_not_allowed`, producing repeated empty retries and HTTP 400 errors. The current working tree contains a regression fix for this behavior.

### 2.4 Completion and process state were coupled

A child can have a durable final output while waiting for a parent message, while the OS process is still alive. Conversely, a parent close can terminate the process after the output is already persisted. These are separate facts and must be represented separately.

## 3. Pi to GA mapping

| Pi mechanism | GA target |
|---|---|
| `--no-session` | Default `fork_turns=none`; parent context requires explicit opt-in |
| Agent frontmatter tools | Role/capability profile generates static allowlist and schema |
| Remove `subagent` tool | Child schema removes orchestration tools unless explicitly enabled |
| single/parallel/chain | Keep GA APIs but add hard validation and explicit mode metadata |
| Structured result | State, artifact, output, and events agree on completion and errors |
| Output cap | Parent result reads are bounded while full artifacts remain auditable |
| Abort propagation | Stop child process without deleting persisted result |

## 4. Non-negotiable hard constraints

1. Default spawn creates an isolated context.
2. Default child schema has no orchestration tools.
3. Internal sentinels such as `no_tool` bypass business allowlists.
4. Tool boundaries are enforced by schema, permission policy, and process metadata; prompts are not the sole control.
5. Persisted output and artifacts survive parent close.
6. Startup, turn, permission, tool, LLM, and shutdown failures record a stage.
7. Context transfer is explicit through messages, artifacts, or chain placeholders.
8. Existing workflows that need inherited history must declare it and record the reason.

## 5. Conclusion

GA should stop trying to make children stable by adding more prompt text. Pi is stable because it combines independent sessions, static capabilities, explicit scheduling, structured lifecycle, and bounded results. GA should first remove implicit history and recursive capabilities, then make role metadata authoritative, and finally unify the subagent, team, and workflow lifecycle contract.
