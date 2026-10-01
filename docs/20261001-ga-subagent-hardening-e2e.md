# GA Subagent Hardening Real Acceptance

Date: 2026-10-01

Model: `deepseek-v4.1-flash`

Profile: `deepseek-v4.1-flash` (llm.yaml profile index 12)

This report records the post-hardening acceptance run. It intentionally stores
only status and metadata; no API key, authorization header, prompt transcript,
or raw model response is persisted here.

## 1. Full unit regression

Command:

```text
python -m unittest discover -s tests
```

Result:

```text
Ran 1075 tests in 163.560s
OK (skipped=3)
```

## 2. Real subagent control-plane guard

Command:

```text
GA_RUN_REAL_API_E2E=1
GA_REAL_API_EXPECTED_MODEL=deepseek-v4.1-flash
GA_REAL_API_EXPECTED_NAME=deepseek-v4.1-flash
python tests/real_subagent_guard_e2e.py
```

Result: `passed=true`, `issues=[]`.

Evidence checks that passed:

- real child process started and emitted output;
- live name conflict was refused without a stray process;
- tool-layer name conflict returned structured guidance;
- replayed follow-up queued exactly one turn;
- replayed resume returned the same PID and run id;
- closed-name reuse allocated a new task name;
- original output/artifact remained intact;
- 21 recorded spawn-rejection events were observed.

## 3. Real dynamic workflow forward matrix

Command:

```text
GA_RUN_REAL_FORWARD_MATRIX=1
GA_FORWARD_MATRIX_PROFILE=deepseek-v4.1-flash
python tests/real_workflow_forward_matrix_e2e.py
```

Result: `passed=true`, duration `17.4s`, `issues=[]`.

All six cases passed:

- `direct`: validated as single-turn;
- `workflow`: two dependency-ordered waves, all jobs succeeded, integration accepted, final audit passed;
- `delegated`: three bounded jobs in two waves, all jobs succeeded;
- `fallback`: deterministic fallback retained an explicit reason;
- `approval`: remained at the explicit approval gate;
- `eval_contract`: verification schema and strict schema reference were retained.

## 4. Real isolated-capability child

A real child was spawned with:

```json
{
  "role_tools": ["file_read"],
  "allow_delegation": false
}
```

Observed spawn metadata:

```json
{
  "capability_profile": "isolated",
  "context_mode": "isolated",
  "allow_delegation": false
}
```

The child returned the expected marker `CAPABILITY_REAL_OK`. Its persisted
state reported:

```json
{
  "turn_status": "completed",
  "process_status": "waiting_reply",
  "completion_status": "completed",
  "last_error_stage": null
}
```

No orchestration tool call was observed. The child was then closed by the
acceptance harness and its persisted result was retained.

## Conclusion

The Pi-aligned hardening changes are validated at three levels: unit contracts,
real process/control-plane behavior, and real DeepSeek workflow execution. The
remaining optimization work is performance profiling across model latency,
MCP latency, and GA scheduling; it is separate from the correctness boundary
validated here.
