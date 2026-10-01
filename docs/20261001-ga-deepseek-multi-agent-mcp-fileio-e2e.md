# DeepSeek Multi-Agent MCP + File I/O E2E

Date: 2026-10-01

Model/profile: `deepseek-v4.1-flash`

## Test objective

Run a real multi-agent, multi-step workflow with:

1. a research agent calling the real Tavily MCP tool;
2. a coding agent loading the existing `using-superpowers` skill, writing a
   Python file, and reading it back;
3. a synthesis agent combining the two results.

The test workspace was temporary and no credentials or protected files were
read or written.

## Command

```text
GA_RUN_REAL_API_E2E=1
GA_RUN_REAL_MCP_E2E=1
GA_WORKFLOW_LLM_PROFILE=deepseek-v4.1-flash
GA_REAL_API_EXPECTED_MODEL=deepseek-v4.1-flash
GA_REAL_API_EXPECTED_NAME=deepseek-v4.1-flash
python tests/real_complex_workflow_mcp_skill_coding_e2e.py
```

## Observed runtime evidence

The workflow runtime completed successfully:

- runtime status: `succeeded`;
- job count: 3;
- all three jobs: `succeeded`;
- workflow progress entries: 3;
- real MCP discovery found 25 tools;
- `mcp__tavily__tavily_search` was called and returned;
- the coding agent called `load_skill`, `file_write`, and `file_read`;
- the synthesis agent completed without tools;
- the expected temporary Python file was written and contained the requested
  function and prefix.

The research agent produced two MCP tool-call events because the first MCP
request was transiently retried; both calls were recorded as allowed and the
second returned successfully. No tool was denied.

## Planner gate finding

The real DeepSeek planner output was rejected by the hard workflow validator
with:

```text
missing_verification_role
incomplete_acceptance_contract
```

The generated plan contained research, coding, and synthesis labels, but did
not declare a verification agent and did not include both required coding
acceptance checks (`python_unittest` and `verification_schema`). The test script
therefore returned `passed=false` even though its fixed runtime script completed
all three agents successfully.

This is an important distinction:

- subagent/runtime/MCP/file-I/O execution: passed;
- real planner-to-validator contract: failed and correctly blocked.

The result is not a subagent transport failure. It is a DeepSeek planner
contract-completeness issue and should be addressed by strengthening the
planner repair prompt or deterministic plan normalizer, then rerunning this
same E2E.
