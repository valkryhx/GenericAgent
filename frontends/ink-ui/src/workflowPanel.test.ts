import test from 'node:test'
import assert from 'node:assert/strict'
import { workflowPanelFromDetail, workflowPanelRows, workflowPanelCommandForKey } from './workflowPanel.js'
import type { WorkflowRun } from './protocol.js'

const awaitingRun: WorkflowRun = {
  runId: 'wf_demo',
  sessionId: 'session',
  status: 'awaiting_approval',
  permissionProfile: 'inherit-current-permissions',
  jobs: [],
}

test('workflowPanelFromDetail exposes raw script and approval metadata rows', () => {
  const panel = workflowPanelFromDetail({
    run: awaitingRun,
    script: 'export const meta = { name: "demo" }\nreturn { ok: true }',
    events: [],
  })

  assert.deepEqual(workflowPanelRows(panel), [
    'Workflow wf_demo - awaiting_approval',
    'Permission: inherit-current-permissions',
    'Jobs: 0',
    'Script:',
    'export const meta = { name: "demo" }',
    'return { ok: true }',
    'Enter approve - d deny - s stop - Esc close',
  ])
})

test('workflowPanelCommandForKey maps approval keyboard shortcuts', () => {
  const panel = workflowPanelFromDetail({ run: awaitingRun, script: 'return 1', events: [] })

  assert.deepEqual(workflowPanelCommandForKey(panel, { return: true }, ''), {
    type: 'workflow_approve',
    runId: 'wf_demo',
  })
  assert.deepEqual(workflowPanelCommandForKey(panel, {}, 'd'), {
    type: 'workflow_deny',
    runId: 'wf_demo',
    reason: 'denied from Ink UI',
  })
  assert.deepEqual(workflowPanelCommandForKey({ ...panel, run: { ...awaitingRun, status: 'running' } }, {}, 's'), {
    type: 'workflow_stop',
    runId: 'wf_demo',
    reason: 'stopped from Ink UI',
  })
})
