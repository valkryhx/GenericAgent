import type { BridgeCommand, WorkflowDraftPayload, WorkflowEvent, WorkflowProgressEntry, WorkflowProgressPayload, WorkflowRun, WorkflowJob } from './protocol.js'
import type { InputKey } from './inputController.js'

export type WorkflowDetailPayload = {
  run: WorkflowRun
  script: string
  events: WorkflowEvent[]
  draft?: WorkflowDraftPayload | null
  progress?: WorkflowProgressPayload | null
}

export type WorkflowOverviewAgent = {
  id: string
  label: string
  status: string
  model?: string
  tokenText?: string
  toolCount: number
}

export type WorkflowOverviewPhase = {
  title: string
  agents: WorkflowOverviewAgent[]
  completed: number
  total: number
}

export type WorkflowOverview = {
  run: WorkflowRun
  name: string
  description: string
  status: string
  phases: WorkflowOverviewPhase[]
  selectedPhase: number
  completed: number
  total: number
}

export type WorkflowPanelState =
  | {
    mode?: 'detail'
    run: WorkflowRun
    script: string
    events: WorkflowEvent[]
  }
  | WorkflowListPanelState
  | WorkflowOverviewPanelState

export type WorkflowOverviewPanelState = {
  mode: 'overview'
  overview: WorkflowOverview
}

export type WorkflowListPanelState = {
  mode: 'list'
  runs: WorkflowRun[]
  selected: number
}

export type WorkflowListDecision = {
  panel?: WorkflowListPanelState
  command?: BridgeCommand
}

export function workflowPanelFromDetail(detail: WorkflowDetailPayload): Extract<WorkflowPanelState, { mode: 'overview' }> {
  return {
    mode: 'overview',
    overview: workflowOverviewFromDetail(detail),
  }
}

export function workflowRawDetailPanelFromDetail(detail: { run: WorkflowRun; script: string; events: WorkflowEvent[] }): Extract<WorkflowPanelState, { run: WorkflowRun }> {
  return {
    mode: 'detail',
    run: detail.run,
    script: detail.script,
    events: detail.events,
  }
}

export function workflowListPanelFromRuns(runs: WorkflowRun[]): WorkflowListPanelState {
  return { mode: 'list', runs, selected: 0 }
}

export function workflowListRows(panel: WorkflowListPanelState): string[] {
  const completed = panel.runs.filter(run => run.status === 'succeeded').length
  const completedLabel = `${completed} completed`
  return [
    'Dynamic workflows',
    completedLabel,
    '',
    ...panel.runs.map((run, index) => workflowListRunRow(run, index === panel.selected)),
    'Enter view - Up/Down move - Esc close',
  ]
}

function workflowListRunRow(run: WorkflowRun, selected: boolean): string {
  const cursor = selected ? '›' : ' '
  const icon = workflowStatusIcon(run.status)
  const name = workflowDisplayName(run)
  const agentCount = run.jobs?.length ?? 0
  return `${cursor} ${icon} ${name}  ${agentCount} ${agentCount === 1 ? 'agent' : 'agents'}`
}

function workflowDisplayName(run: WorkflowRun): string {
  const metadataName = run.metadata?.workflowName
  if (typeof metadataName === 'string' && metadataName.trim()) return metadataName.trim()
  const taskType = run.metadata?.workflowTaskType
  if (typeof taskType === 'string' && taskType.trim()) return taskType.trim()
  return run.runId
}

function workflowStatusIcon(status: string): string {
  if (status === 'succeeded') return '✓'
  if (status === 'running') return '◌'
  if (status === 'awaiting_approval') return '◌'
  if (status === 'failed' || status === 'killed' || status === 'cancelled') return '✗'
  return '·'
}

export function workflowListCommandForKey(
  panel: WorkflowListPanelState,
  key: InputKey,
  _rawInput: string,
): WorkflowListDecision | null {
  if (key.upArrow) {
    return { panel: { ...panel, selected: Math.max(0, panel.selected - 1) } }
  }
  if (key.downArrow) {
    return { panel: { ...panel, selected: Math.min(Math.max(0, panel.runs.length - 1), panel.selected + 1) } }
  }
  if (key.return) {
    const selected = panel.runs[panel.selected]
    return selected ? { command: { type: 'workflow_detail', runId: selected.runId } } : null
  }
  return null
}

export function workflowOverviewFromDetail(detail: WorkflowDetailPayload, selectedPhase = 0): WorkflowOverview {
  const run = detail.run
  const draft = detail.draft ?? null
  const progressEntries = detail.progress?.workflowProgress ?? []
  const jobs = run.jobs ?? []
  const draftPhaseByLabel = draftPhaseLookup(draft)
  const phases = new Map<string, WorkflowOverviewAgent[]>()

  for (const entry of progressEntries) {
    const label = entry.label || entry.jobId || entry.agentId || 'agent'
    const phaseTitle = entry.phaseTitle || entry.phase || draftPhaseByLabel.get(label) || '未分阶段'
    const agent = overviewAgentFromProgress(entry, jobs.find(job => job.jobId === entry.jobId || job.metadata?.label === label))
    if (!phases.has(phaseTitle)) phases.set(phaseTitle, [])
    phases.get(phaseTitle)!.push(agent)
  }

  if (phases.size === 0) {
    for (const job of jobs) {
      const label = jobLabel(job)
      const phaseTitle = job.phase || draftPhaseByLabel.get(label) || '未分阶段'
      if (!phases.has(phaseTitle)) phases.set(phaseTitle, [])
      phases.get(phaseTitle)!.push(overviewAgentFromJob(job))
    }
  }

  if (phases.size === 0) {
    for (const phase of draft?.plan.phases ?? []) {
      const title = phase.title || '未分阶段'
      const agents = (phase.agents ?? []).map(agent => ({
        id: agent.label || 'agent',
        label: agent.label || 'agent',
        status: 'registered',
        toolCount: 0,
      }))
      if (agents.length > 0) phases.set(title, agents)
    }
  }

  const phaseList = Array.from(phases.entries()).map(([title, agents]) => ({
    title,
    agents,
    completed: agents.filter(agent => agent.status === 'succeeded' || agent.status === 'cached').length,
    total: agents.length,
  }))
  const total = phaseList.reduce((sum, phase) => sum + phase.total, 0)
  const completed = phaseList.reduce((sum, phase) => sum + phase.completed, 0)
  const clampedSelected = Math.min(Math.max(0, selectedPhase), Math.max(0, phaseList.length - 1))
  return {
    run,
    name: workflowOverviewName(run, draft),
    description: workflowOverviewDescription(run, draft),
    status: run.status === 'succeeded' ? 'done' : run.status,
    phases: phaseList,
    selectedPhase: clampedSelected,
    completed,
    total,
  }
}

export function workflowOverviewRows(overview: WorkflowOverview): string[] {
  const selectedPhase = overview.phases[overview.selectedPhase]
  const rows = [
    `${overview.name}  ${overview.completed}/${overview.total} agents · ${overview.status}`,
    overview.description,
    `Phases | ${selectedPhase ? `${selectedPhase.title} · ${selectedPhase.total} ${selectedPhase.total === 1 ? 'agent' : 'agents'}` : 'Agents'}`,
  ]
  const maxRows = Math.max(overview.phases.length, selectedPhase?.agents.length ?? 0)
  for (let index = 0; index < maxRows; index++) {
    const phase = overview.phases[index]
    const agent = selectedPhase?.agents[index]
    rows.push(`${phase ? phaseRow(phase, index === overview.selectedPhase) : ''.padEnd(16)} | ${agent ? overviewAgentRow(agent) : ''}`)
  }
  rows.push('Enter agent - Up/Down phase - Esc back')
  return rows
}

function draftPhaseLookup(draft: WorkflowDraftPayload | null): Map<string, string> {
  const result = new Map<string, string>()
  for (const phase of draft?.plan.phases ?? []) {
    const title = phase.title || '未分阶段'
    for (const agent of phase.agents ?? []) {
      if (agent.label) result.set(agent.label, title)
    }
  }
  return result
}

function overviewAgentFromProgress(entry: WorkflowProgressEntry, job?: WorkflowJob): WorkflowOverviewAgent {
  const label = stringMetadata(entry.label) || stringMetadata(job?.metadata?.label) || stringMetadata(entry.jobId) || stringMetadata(entry.agentId) || 'agent'
  return {
    id: stringMetadata(entry.jobId) || stringMetadata(entry.agentId) || job?.jobId || label,
    label,
    status: entry.state || job?.status || 'registered',
    model: stringMetadata(job?.metadata?.model),
    tokenText: formatTokenUsage(entry.tokenUsage) ?? undefined,
    toolCount: entry.toolCalls?.length ?? 0,
  }
}

function overviewAgentFromJob(job: WorkflowJob): WorkflowOverviewAgent {
  return {
    id: job.jobId,
    label: jobLabel(job),
    status: job.status,
    model: stringMetadata(job.metadata?.model),
    tokenText: formatTokenUsage(job.metadata?.tokenUsage) ?? undefined,
    toolCount: Array.isArray(job.metadata?.toolCalls) ? job.metadata.toolCalls.length : 0,
  }
}

function jobLabel(job: WorkflowJob): string {
  const label = job.metadata?.label
  return typeof label === 'string' && label.trim() ? label.trim() : job.jobId
}

function workflowOverviewName(run: WorkflowRun, draft: WorkflowDraftPayload | null): string {
  const metadataName = stringMetadata(run.metadata?.workflowName)
  if (metadataName) return metadataName
  const draftName = draft?.plan.meta?.name
  if (draftName) return draftName
  return workflowDisplayName(run)
}

function workflowOverviewDescription(run: WorkflowRun, draft: WorkflowDraftPayload | null): string {
  const metadataDescription = stringMetadata(run.metadata?.workflowDescription)
  if (metadataDescription) return metadataDescription
  const draftDescription = draft?.plan.meta?.description
  if (draftDescription) return draftDescription
  const taskType = stringMetadata(run.metadata?.workflowTaskType)
  return taskType || ''
}

function phaseRow(phase: WorkflowOverviewPhase, selected: boolean): string {
  return `${selected ? '›' : ' '} ✓ ${phase.title} ${phase.completed}/${phase.total}`
}

function overviewAgentRow(agent: WorkflowOverviewAgent): string {
  const stats = [agent.tokenText, agent.toolCount ? `${agent.toolCount} tools` : null].filter(Boolean).join(' · ')
  return `${workflowStatusIcon(agent.status)} ${agent.label}${stats ? `  ${stats}` : ''}`
}

function formatTokenUsage(value: unknown): string | null {
  if (!value || typeof value !== 'object') return null
  const usage = value as Record<string, unknown>
  const total = numberValue(usage.totalTokens) ?? numberValue(usage.total_tokens) ?? numberValue(usage.total)
  if (total === null) return null
  if (total >= 1000) {
    const rounded = Math.round(total / 100) / 10
    return `${Number.isInteger(rounded) ? rounded.toFixed(0) : rounded}k tok`
  }
  return `${total} tok`
}

function numberValue(value: unknown): number | null {
  return typeof value === 'number' && Number.isFinite(value) ? value : null
}

function stringMetadata(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value.trim() : undefined
}

export function workflowPanelRows(panel: WorkflowPanelState): string[] {
  if (panel.mode === 'list') return workflowListRows(panel)
  if (panel.mode === 'overview') return workflowOverviewRows(panel.overview)
  const permission = panel.run.permissionProfile || '(default)'
  const jobs = panel.run.jobs?.length ?? 0
  const scriptLines = panel.script.split('\n')
  const controls = resumeableWorkflowStatuses.has(panel.run.status)
    ? 'Enter approve - r resume - d deny - s stop - Esc close'
    : 'Enter approve - d deny - s stop - Esc close'
  return [
    `Workflow ${panel.run.runId} - ${panel.run.status}`,
    `Permission: ${permission}`,
    `Jobs: ${jobs}`,
    'Script:',
    ...(scriptLines.length > 0 ? scriptLines : ['']),
    controls,
  ]
}

const resumeableWorkflowStatuses = new Set(['failed', 'killed', 'interrupted', 'succeeded'])

export function workflowPanelCommandForKey(
  panel: WorkflowPanelState,
  key: InputKey,
  rawInput: string,
): BridgeCommand | null {
  if (panel.mode === 'list') {
    return workflowListCommandForKey(panel, key, rawInput)?.command ?? null
  }
  if (panel.mode === 'overview') return null
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
  if (lowered === 'r' && resumeableWorkflowStatuses.has(panel.run.status)) {
    return { type: 'workflow_resume', runId: panel.run.runId }
  }
  return null
}
