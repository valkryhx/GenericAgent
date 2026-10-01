import type { BridgeCommand, WorkflowJob, WorkflowProgressEntry, WorkflowRun } from './protocol.js'
import type { AppState } from './state.js'
import type { InputKey } from './inputController.js'

export type WorkflowStatusBarSummary = {
  runId: string
  status: string
  name: string
  completedAgents: number
  totalAgents: number
  activeAgent?: string
  lastActivity?: string
  tokenText?: string
  integrationText?: string
}

const liveStatuses = new Set(['running', 'awaiting_approval'])
const terminalStatuses = new Set(['succeeded', 'failed', 'cancelled', 'killed', 'interrupted', 'partial'])
export function workflowStatusBarFromState(state: AppState): WorkflowStatusBarSummary | null {
  for (let index = state.workflows.length - 1; index >= 0; index--) {
    const run = state.workflows[index]!
    const commonRecord = state.agents.find(record => record.recordKind === 'workflow_run' && record.runId === run.runId)
    const status = commonRecord?.status ?? run.status
    if (liveStatuses.has(status) || status === 'partial') return workflowStatusBarFromRun(run, state, status)
    if (!terminalStatuses.has(status)) continue
    if (!workflowIntegrationText(run, state)) continue
    return workflowStatusBarFromRun(run, state, status)
  }
  return null
}

function workflowStatusBarFromRun(run: WorkflowRun, state: AppState, status = run.status): WorkflowStatusBarSummary {
  const progressEntries = state.workflowDetails[run.runId]?.progress?.workflowProgress ?? []
  const agents = progressEntries.length > 0 ? progressEntries : progressFromJobs(run.jobs ?? [])
  const completedAgents = agents.filter(agent => agent.state === 'succeeded' || agent.state === 'cached').length
  const totalAgents = agents.length
  const active = agents.find(agent => agent.state === 'running') ?? agents.find(agent => agent.state === 'queued' || agent.state === 'registered')
  const summary: WorkflowStatusBarSummary = {
    runId: run.runId,
    status,
    name: workflowDisplayName(run),
    completedAgents,
    totalAgents,
    activeAgent: labelForProgress(active),
    lastActivity: typeof active?.lastToolName === 'string' && active.lastToolName.trim() ? active.lastToolName.trim() : undefined,
    tokenText: formatTokenUsage(sumTokenUsage(agents)),
  }
  const integrationText = workflowIntegrationText(run, state)
  if (integrationText) summary.integrationText = integrationText
  return summary
}

function workflowIntegrationText(run: WorkflowRun, state: AppState): string | undefined {
  const progress = state.workflowDetails[run.runId]?.progress
  const integration = stringValue(progress?.integrationStatus) || stringValue(run.metadata?.integrationStatus)
  const audit = stringValue(progress?.finalAuditStatus) || stringValue(run.metadata?.finalAuditStatus)
  const pieces = [
    integration ? `integration ${integration}` : null,
    audit ? `audit ${audit}` : null,
  ].filter(Boolean)
  return pieces.length > 0 ? pieces.join(' · ') : undefined
}

function progressFromJobs(jobs: WorkflowJob[]): WorkflowProgressEntry[] {
  return jobs.map(job => ({
    label: stringValue(job.metadata?.label),
    state: job.status,
    tokenUsage: recordValue(job.metadata?.tokenUsage),
  }))
}

export function workflowStatusBarRows(bar: WorkflowStatusBarSummary): string[] {
  if (bar.status === 'awaiting_approval') {
    // Legacy status only; product path always auto-starts workflows.
    return ['Enter view', `› ◌ ${bar.name}  awaiting approval`]
  }
  const pieces = [`${bar.completedAgents}/${bar.totalAgents} agents done`]
  if (bar.activeAgent) pieces.push(bar.lastActivity ? `${bar.activeAgent}: ${bar.lastActivity}` : bar.activeAgent)
  if (bar.tokenText) pieces.push(bar.tokenText)
  if (bar.integrationText) pieces.push(bar.integrationText)
  const icon = bar.status === 'succeeded' ? '✓' : bar.status === 'failed' ? '✗' : '◌'
  return [bar.status === 'running' ? 'Enter view · x stop' : 'Enter view', `› ${icon} ${bar.name}  ${pieces.filter(Boolean).join(' · ')}`]
}

export function workflowStatusBarCommandForKey(
  bar: WorkflowStatusBarSummary,
  key: InputKey,
  rawInput: string,
): BridgeCommand | null {
  if (key.return) return { type: 'workflow_detail', runId: bar.runId }
  if (rawInput.toLowerCase() === 'x' && bar.status === 'running' && isPlainKey(key)) {
    return { type: 'workflow_stop', runId: bar.runId, reason: 'stopped from Ink UI status bar' }
  }
  return null
}

function isPlainKey(key: InputKey): boolean {
  return !key.ctrl && !key.meta && !key.shift
}

function workflowDisplayName(run: WorkflowRun): string {
  const metadataName = stringValue(run.metadata?.workflowName)
  if (metadataName) return metadataName
  const taskType = stringValue(run.metadata?.workflowTaskType)
  return taskType || run.runId
}

function labelForJob(job: WorkflowJob): string {
  return stringValue(job.metadata?.label) || job.jobId
}

function labelForProgress(entry?: WorkflowProgressEntry): string | undefined {
  if (!entry) return undefined
  return stringValue(entry.label) || stringValue(entry.jobId) || stringValue(entry.agentId)
}

function sumTokenUsage(entries: WorkflowProgressEntry[]): number | null {
  let total = 0
  let found = false
  for (const entry of entries) {
    const value = tokenTotal(entry.tokenUsage)
    if (value !== null) {
      total += value
      found = true
    }
  }
  return found ? total : null
}

function tokenTotal(value: unknown): number | null {
  if (!value || typeof value !== 'object') return null
  const usage = value as Record<string, unknown>
  return numberValue(usage.totalTokens) ?? numberValue(usage.total_tokens) ?? numberValue(usage.total)
}

function formatTokenUsage(total: number | null): string | undefined {
  if (total === null) return undefined
  if (total >= 1000) {
    const rounded = Math.round(total / 100) / 10
    return `${Number.isInteger(rounded) ? rounded.toFixed(0) : rounded}k tok`
  }
  return `${total} tok`
}

function numberValue(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function recordValue(value: unknown): Record<string, unknown> | undefined {
  return value && typeof value === 'object' && !Array.isArray(value) ? value as Record<string, unknown> : undefined
}

function stringValue(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value.trim() : undefined
}
