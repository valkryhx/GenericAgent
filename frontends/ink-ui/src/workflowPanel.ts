import type { BridgeCommand, WorkflowEvent, WorkflowRun } from './protocol.js'
import type { InputKey } from './inputController.js'

export type WorkflowPanelState = {
  run: WorkflowRun
  script: string
  events: WorkflowEvent[]
}

export function workflowPanelFromDetail(detail: { run: WorkflowRun; script: string; events: WorkflowEvent[] }): WorkflowPanelState {
  return {
    run: detail.run,
    script: detail.script,
    events: detail.events,
  }
}

export function workflowPanelRows(panel: WorkflowPanelState): string[] {
  const permission = panel.run.permissionProfile || '(default)'
  const jobs = panel.run.jobs?.length ?? 0
  const scriptLines = panel.script.split('\n')
  return [
    `Workflow ${panel.run.runId} - ${panel.run.status}`,
    `Permission: ${permission}`,
    `Jobs: ${jobs}`,
    'Script:',
    ...(scriptLines.length > 0 ? scriptLines : ['']),
    'Enter approve - d deny - s stop - Esc close',
  ]
}

export function workflowPanelCommandForKey(
  panel: WorkflowPanelState,
  key: InputKey,
  rawInput: string,
): BridgeCommand | null {
  if (key.return && panel.run.status === 'awaiting_approval') {
    return { type: 'workflow_approve', runId: panel.run.runId }
  }
  const lowered = rawInput.toLowerCase()
  if ((lowered === 'd' || lowered === 'n') && panel.run.status === 'awaiting_approval') {
    return { type: 'workflow_deny', runId: panel.run.runId, reason: 'denied from Ink UI' }
  }
  if (lowered === 's' && panel.run.status === 'running') {
    return { type: 'workflow_stop', runId: panel.run.runId, reason: 'stopped from Ink UI' }
  }
  return null
}
