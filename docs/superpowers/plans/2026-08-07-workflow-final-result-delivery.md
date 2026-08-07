# Workflow Final Result Delivery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persist terminal workflow `result_ref` values so the GA Ink bridge and reducer receive the real final artifact for succeeded, failed, and killed runs.

**Architecture:** Keep `WorkflowStore.write_final_result()` as a low-level artifact writer and preserve the existing pre-artifact `save_run()` calls that enforce the external-kill guard. Add one post-artifact `save_run()` in each `WorkflowRuntime` terminal branch, then cover the durable state boundary, the real Node-worker/bridge seam, and an opt-in real `gpt-5.6-luna` bridge-plus-Ink-reducer E2E.

**Tech Stack:** Python 3.10–3.13, standard-library `unittest`, existing Node workflow worker, React/Ink TypeScript state reducer, `tsx`, and `llm.yaml` profile `luna` (`gpt-5.6-luna`, provider `gpt-super-responses`).

---

## File map

- `tests/test_workflow_runtime.py`: regression assertions that terminal `result_ref` survives `WorkflowStore.load_run()` for success, failure, and external kill.
- `tests/test_ink_bridge.py`: real `WorkflowRuntime` + real Node worker + `FakeChildAgentRunner` seam proving `workflow_final` reads the durable artifact instead of returning `missing_ref`.
- `workflow_runtime.py`: minimal three-branch persistence fix after `write_final_result()`.
- `tests/real_ink_workflow_final_delivery_e2e.ts`: opt-in real bridge-client/Ink-reducer E2E using UI model switching to `luna` and a real workflow child.
- `docs/workflow_defect_2026_08_05.md`: defect status, TDD evidence, deterministic regression evidence, and fresh real API/UI result.

### Task 1: Add durable terminal-state regression tests

**Files:**
- Modify: `tests/test_workflow_runtime.py:179-193`
- Modify: `tests/test_workflow_runtime.py:354-381`
- Modify: `tests/test_workflow_runtime.py:1958-1990`

- [x] **Step 1: Assert failed runs persist the final artifact reference**

Add the following assertion to `assert_runtime_failed_with_marker()` immediately after `self.assertEqual("failed", loaded.status)`:

```python
self.assertEqual("final-result.json", loaded.result_ref)
```

- [x] **Step 2: Assert succeeded runs persist the final artifact reference**

In `test_runtime_executes_phase_log_and_agent_script_with_fake_runner()`, reload the run after `runtime.run(run)` and assert the durable reference:

```python
loaded = store.load_run("wf_test")
self.assertEqual("succeeded", loaded.status)
self.assertEqual("final-result.json", loaded.result_ref)
```

- [x] **Step 3: Assert externally killed runs persist the final artifact reference**

In `test_runtime_observes_external_kill_state()`, add this assertion immediately after the existing killed-status assertion:

```python
self.assertEqual("final-result.json", loaded.result_ref)
```

- [x] **Step 4: Run the three runtime tests and verify RED**

Run:

```powershell
python -m unittest `
  tests.test_workflow_runtime.WorkflowRuntimeTest.test_runtime_executes_phase_log_and_agent_script_with_fake_runner `
  tests.test_workflow_runtime.WorkflowRuntimeTest.test_runtime_marks_run_failed_when_worker_errors `
  tests.test_workflow_runtime.WorkflowRuntimeTest.test_runtime_observes_external_kill_state -v
```

Expected: all three tests fail only because the reloaded `WorkflowRun.result_ref` is `None` instead of `"final-result.json"`.

### Task 2: Add the bridge delivery regression test

**Files:**
- Modify: `tests/test_ink_bridge.py:25`
- Modify: `tests/test_ink_bridge.py` near the existing workflow final tests

- [x] **Step 1: Import the deterministic child runner**

Replace:

```python
from workflow_child_agent import AgentResult  # noqa: E402
```

with:

```python
from workflow_child_agent import AgentResult, FakeChildAgentRunner  # noqa: E402
```

- [x] **Step 2: Add a real runtime-to-bridge seam test**

Add this test near the existing `workflow_final` tests:

```python
def test_workflow_final_reads_persisted_artifact_from_real_runtime(self):
    agent = FakeAgent()
    agent.session_id = "session_workflow"
    events = []
    marker = "GA_WORKFLOW_FINAL_DELIVERY_OK"
    script = f"""
const child = await agent('return the deterministic marker')
return {{ marker: child.summary }}
"""

    with tempfile.TemporaryDirectory() as tmp:
        bridge = GenericAgentBridge(
            agent_factory=lambda: agent,
            emit=events.append,
            workflow_root=tmp,
            workflow_runtime_factory=lambda **kwargs: WorkflowRuntime(
                runner=FakeChildAgentRunner(results={"agent_1": {"summary": marker}}),
                **kwargs,
            ),
        )
        run_id = bridge.workflow_draft(script)
        self.assertTrue(bridge.workflow_approve(run_id, timeout_seconds=5.0))
        bridge.wait_for_workflow_idle(run_id, timeout=5)

        loaded = bridge.workflow_store.load_run(run_id)
        self.assertEqual("succeeded", loaded.status)
        self.assertEqual("final-result.json", loaded.result_ref)
        final_event = next(
            event
            for event in events
            if event["type"] == "workflow_final" and event["runId"] == run_id
        )
        self.assertEqual(marker, final_event["result"]["result"]["marker"])
        self.assertNotIn("artifactError", final_event["result"])
        self.assert_bridge_idle_tail(events)
```

- [x] **Step 3: Run the bridge test and verify RED**

Run:

```powershell
python -m unittest tests.test_ink_bridge.InkBridgeTest.test_workflow_final_reads_persisted_artifact_from_real_runtime -v
```

Expected: FAIL because `loaded.result_ref` is `None`; before that assertion is added, the corresponding bridge payload would contain `artifactError: "missing_ref"` and no marker.

### Task 3: Persist `result_ref` in every runtime terminal path

**Files:**
- Modify: `workflow_runtime.py:177-211`

- [x] **Step 1: Save the succeeded run after writing its final artifact**

Change the success branch to:

```python
self.store.write_final_result(run, final_payload)
self.store.save_run(run)
return WorkflowRuntimeResult(run=run, result=result, logs=list(self._logs), phases=list(self._phases))
```

- [x] **Step 2: Save the killed run after writing its final artifact**

Change the killed branch to:

```python
self.store.write_final_result(
    run,
    self._final_payload(run, "killed", result=self._last_worker_result, error=run.error),
)
self.store.save_run(run)
self._append(run, "workflow_killed", {"error": run.error})
```

- [x] **Step 3: Save the failed run after writing its final artifact**

Change the failed branch to:

```python
self.store.write_final_result(
    run,
    self._final_payload(run, "failed", result=self._last_worker_result, error=reason),
)
self.store.save_run(run)
self._append(run, "workflow_failed", {"error": reason})
```

- [x] **Step 4: Run the RED tests and verify GREEN**

Run:

```powershell
python -m unittest `
  tests.test_workflow_runtime.WorkflowRuntimeTest.test_runtime_executes_phase_log_and_agent_script_with_fake_runner `
  tests.test_workflow_runtime.WorkflowRuntimeTest.test_runtime_marks_run_failed_when_worker_errors `
  tests.test_workflow_runtime.WorkflowRuntimeTest.test_runtime_observes_external_kill_state `
  tests.test_ink_bridge.InkBridgeTest.test_workflow_final_reads_persisted_artifact_from_real_runtime -v
```

Expected: 4 tests pass; the bridge final payload contains the marker and has no `artifactError`.

- [x] **Step 5: Commit the focused code and deterministic tests**

```powershell
git add workflow_runtime.py tests/test_workflow_runtime.py tests/test_ink_bridge.py
git commit -m "fix(workflow): persist final result references"
```

### Task 4: Add and run the real Ink delivery E2E

**Files:**
- Create: `tests/real_ink_workflow_final_delivery_e2e.ts`

- [x] **Step 1: Add an opt-in bridge-client/reducer harness**

Create `tests/real_ink_workflow_final_delivery_e2e.ts` with this complete implementation:

```typescript
import { readFileSync } from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { startBridge, type BridgeClient } from '../frontends/ink-ui/src/bridgeClient.js'
import { applyBridgeEvent, initialState, type AppState } from '../frontends/ink-ui/src/state.js'
import type { BridgeEvent } from '../frontends/ink-ui/src/protocol.js'

const REPO = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..')
const BRIDGE_SCRIPT = path.join(REPO, 'frontends', 'ink_bridge.py')
const PYTHON = process.env.PYTHON || 'python'
const OPT_IN = process.env.GA_RUN_REAL_INK_WORKFLOW_FINAL_E2E === '1'
const TARGET_SELECTOR = 'luna'
const EXPECTED_MODEL = 'luna/gpt-5.6-luna'
const EXPECTED_PROVIDER = 'gpt-super-responses'
const MARKER = 'GA_INK_WORKFLOW_FINAL_LUNA_OK_20260807'
const WORKFLOW_SCRIPT = `
const result = await agent('不要调用工具，只输出这一行精确文本：${MARKER}', { label: 'ink-final-delivery' })
return { summary: result.summary }
`

function boundedError(value: unknown): string {
  return String(value).replaceAll(REPO, '<repo>').slice(0, 500)
}

async function main(): Promise<number> {
  if (!OPT_IN) {
    console.log(JSON.stringify({
      passed: false,
      skipped: true,
      reason: 'set GA_RUN_REAL_INK_WORKFLOW_FINAL_E2E=1 to run the real Ink workflow delivery E2E',
      model: EXPECTED_MODEL,
      provider: EXPECTED_PROVIDER,
    }, null, 2))
    return 0
  }

  let state: AppState = initialState
  let client: BridgeClient | null = null
  let runId = ''
  let currentModel = ''
  let switchSucceeded = false
  let draftSent = false
  let settled = false
  const eventTypes: string[] = []

  const summary = await new Promise<Record<string, unknown>>(resolve => {
    const finish = (value: Record<string, unknown>) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      resolve(value)
    }
    const fail = (error: unknown) => finish({
      passed: false,
      skipped: false,
      model: currentModel || EXPECTED_MODEL,
      provider: EXPECTED_PROVIDER,
      runId: runId || null,
      error: boundedError(error),
      eventTypes,
    })
    const timer = setTimeout(() => fail('real Ink workflow delivery E2E timed out'), 240_000)

    const onEvent = (event: BridgeEvent) => {
      state = applyBridgeEvent(state, event)
      eventTypes.push(event.type)

      if (event.type === 'error') {
        fail(`${event.code}: ${event.message}`)
        return
      }
      if (event.type === 'ready') {
        client?.send({ type: 'model_switch', selector: TARGET_SELECTOR })
        return
      }
      if (event.type === 'model_switch_result') {
        switchSucceeded = event.ok
        if (!event.ok) fail(event.message)
        return
      }
      if (event.type === 'model_status') {
        currentModel = event.models.find(model => model.current)?.name || ''
        if (switchSucceeded && currentModel === EXPECTED_MODEL && !draftSent) {
          draftSent = true
          client?.send({ type: 'workflow_draft', script: WORKFLOW_SCRIPT })
        }
        return
      }
      if (event.type === 'workflow_draft') {
        runId = event.run.runId
        client?.send({ type: 'workflow_approve', runId, timeoutSeconds: 180 })
        return
      }
      if (event.type !== 'workflow_final' || event.runId !== runId) return

      const run = state.workflows.find(item => item.runId === runId)
      const reducerPayload = state.workflowResults[runId]
      const finalText = JSON.stringify(event.result)
      const reducerText = JSON.stringify(reducerPayload)
      const artifactDir = path.resolve(String(run?.artifactDir || ''))
      const transcriptRef = String(run?.jobs?.[0]?.metadata?.transcriptRef || '')
      const transcriptPath = path.resolve(artifactDir, transcriptRef)
      const transcriptIsBounded = Boolean(
        artifactDir
        && transcriptRef
        && transcriptPath.startsWith(`${artifactDir}${path.sep}`),
      )
      let transcriptContainsMarker = false
      if (transcriptIsBounded) {
        try {
          transcriptContainsMarker = readFileSync(transcriptPath, 'utf8').includes(MARKER)
        } catch {
          transcriptContainsMarker = false
        }
      }
      const checks = {
        modelSelected: currentModel === EXPECTED_MODEL,
        terminalSucceeded: run?.status === 'succeeded',
        persistedResultRef: run?.resultRef === 'final-result.json',
        finalContainsMarker: finalText.includes(MARKER),
        finalHasNoArtifactError: !Object.prototype.hasOwnProperty.call(event.result, 'artifactError'),
        reducerContainsMarker: reducerText.includes(MARKER),
        transcriptContainsMarker,
        commonSnapshotSeen: eventTypes.includes('agent_snapshot'),
        commonEventSeen: eventTypes.includes('agent_event'),
      }
      finish({
        passed: Object.values(checks).every(Boolean),
        skipped: false,
        model: currentModel,
        provider: EXPECTED_PROVIDER,
        runId,
        persistedResultRef: run?.resultRef || null,
        checks,
        eventCount: eventTypes.length,
      })
    }

    client = startBridge(
      PYTHON,
      BRIDGE_SCRIPT,
      onEvent,
      code => {
        if (!settled) fail(`bridge exited before final delivery: ${code}`)
      },
    )
  })

  client?.stop()
  console.log(JSON.stringify(summary, null, 2))
  return summary.passed === true ? 0 : 1
}

main()
  .then(code => {
    process.exitCode = code
  })
  .catch(error => {
    console.log(JSON.stringify({
      passed: false,
      skipped: false,
      model: EXPECTED_MODEL,
      provider: EXPECTED_PROVIDER,
      error: boundedError(error),
    }, null, 2))
    process.exitCode = 1
  })
```

This default path exits before `startBridge()` and therefore cannot make a network request. The real path prints only bounded status/check fields, not prompts, transcript bodies, API metadata, or secrets.

- [x] **Step 2: Verify the default path is a no-network skip**

Run:

```powershell
.\frontends\ink-ui\node_modules\.bin\tsx.cmd tests\real_ink_workflow_final_delivery_e2e.ts
```

Expected: exit 0 with `skipped: true`; no Python bridge is started.

- [x] **Step 3: Run the real UI E2E against `gpt-5.6-luna`**

Run:

```powershell
$env:GA_RUN_REAL_INK_WORKFLOW_FINAL_E2E = "1"
$env:GA_WORKFLOW_LLM_PROFILE = "luna"
.\frontends\ink-ui\node_modules\.bin\tsx.cmd tests\real_ink_workflow_final_delivery_e2e.ts
```

Expected: exit 0 with `passed: true`, `model: "luna/gpt-5.6-luna"`, `provider: "gpt-super-responses"`, `persistedResultRef: "final-result.json"`, and all marker/common-read-model/reducer checks true.

- [x] **Step 4: Commit the reusable real E2E harness**

```powershell
git add tests/real_ink_workflow_final_delivery_e2e.ts
git commit -m "test(ink): cover real workflow final delivery"
```

### Task 5: Run regressions and record the result

**Files:**
- Modify: `docs/workflow_defect_2026_08_05.md`

- [x] **Step 1: Run focused workflow and bridge tests**

```powershell
python -m unittest tests.test_workflow_runtime tests.test_workflow_store tests.test_workflow_controller tests.test_ink_bridge -v
```

Expected: all tests pass.

- [x] **Step 2: Run the P2-1 control-plane regression group**

```powershell
python -m unittest `
  tests.test_agent_runtime_models `
  tests.test_subagent_manager `
  tests.test_agent_control_process `
  tests.test_workflow_models `
  tests.test_workflow_store `
  tests.test_workflow_scheduler `
  tests.test_workflow_controller `
  tests.test_workflow_runtime `
  tests.test_agent_control_workflow `
  tests.test_agent_control -v
```

Expected: all tests pass.

- [x] **Step 3: Run the complete deterministic suite**

```powershell
python -m unittest discover -s tests
```

Expected: exit 0, no failures or errors; only existing opt-in skips.

- [x] **Step 4: Run Ink tests and typecheck**

```powershell
npm test
npm run typecheck
```

Run from `frontends/ink-ui`. Expected: all Node tests pass and TypeScript exits 0.

- [x] **Step 5: Update the defect document**

Append a dated subsection under P2-1 recording:

```text
- The earlier backend-only E2E did not prove UI final-result delivery.
- Root cause: write_final_result mutated only the in-memory run after the terminal save.
- Fix: post-artifact save_run in succeeded/failed/killed runtime branches.
- RED evidence: succeeded/failed/killed durable result_ref and bridge seam all failed with None/missing_ref.
- GREEN/full-suite/Ink/typecheck counts from the fresh commands above.
- Real UI E2E profile/model/provider and checks for child transcript, workflow_final, reducer workflowResults, common snapshot/event, and persisted resultRef.
```

- [x] **Step 6: Commit the verification record**

```powershell
git add docs/workflow_defect_2026_08_05.md docs/superpowers/plans/2026-08-07-workflow-final-result-delivery.md
git commit -m "docs(workflow): record final result delivery verification"
```

## Self-review

- Spec coverage: all three terminal branches, durable reload, bridge final payload, Ink reducer, real child transcript, common snapshot/event, and `gpt-5.6-luna` selection are assigned to explicit tasks.
- Placeholder scan: no unresolved placeholders or unspecified implementation steps remain.
- Type consistency: Python uses `WorkflowRun.result_ref`/serialized `resultRef`; TypeScript uses `WorkflowRun.resultRef` and `workflowResults[runId]`, matching the existing protocol and reducer.
- Scope: no store-side implicit save, bridge fallback guessing, payload schema change, historical-run repair, or workflow executor refactor is included.
