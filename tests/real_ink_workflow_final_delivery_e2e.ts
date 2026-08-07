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
