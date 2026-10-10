import type { BridgeCommand, WorkflowDraftPayload, WorkflowEvent, WorkflowLiveJob, WorkflowProgressEntry, WorkflowProgressPayload, WorkflowRun, WorkflowJob } from './protocol.js'
import type { InputKey } from './inputController.js'

export type WorkflowDetailPayload = {
  run: WorkflowRun
  script: string
  events: WorkflowEvent[]
  draft?: WorkflowDraftPayload | null
  progress?: WorkflowProgressPayload | null
  /** jobId -> live counters published while children still run (UI-only). */
  live?: Record<string, WorkflowLiveJob>
}

/**
 * The workflow panel is a modal overlay under the composer, so its row budget
 * has to be fixed: rendering "all rows" and then letting the caller cut the
 * list produced an 8-row window with no scroll indicator and no way to tell
 * how much content was hidden.
 */
export const WORKFLOW_PANEL_MAX_ROWS = 14
const ACTIVITY_TAIL_ROWS = 12

export type WorkflowOverviewAgent = {
  id: string
  label: string
  status: string
  model?: string
  tokenText?: string
  toolCount: number
  /** Live-only: current turn index and wall-clock elapsed time. */
  turnCount?: number
  elapsedText?: string
  lastToolName?: string
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
  integrationStatus?: string | null
  finalAuditStatus?: string | null
  phases: WorkflowOverviewPhase[]
  selectedPhase: number
  completed: number
  total: number
}

export type WorkflowAgentDetail = {
  id: string
  label: string
  status: string
  statusText: string
  model?: string
  tokenText?: string
  toolCount: number
  turnCount?: number
  elapsedText?: string
  prompt: string
  activityRows: string[]
  activityTotal: number
  outcome: string
}

export type WorkflowPanelState =
  | {
    mode?: 'detail'
    run: WorkflowRun
    script: string
    events: WorkflowEvent[]
    live?: Record<string, WorkflowLiveJob>
  }
  | WorkflowListPanelState
  | WorkflowOverviewPanelState
  | WorkflowAgentDetailPanelState

export type WorkflowOverviewPanelState = {
  mode: 'overview'
  overview: WorkflowOverview
  detailSource: WorkflowDetailPayload
}

export type WorkflowAgentDetailPanelState = {
  mode: 'agent_detail'
  overview: WorkflowOverview
  detailSource: WorkflowDetailPayload
  phaseIndex: number
  agentIndex: number
  scrollOffset: number
  agents: WorkflowOverviewAgent[]
  detail: WorkflowAgentDetail
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

export type WorkflowPanelDecision = {
  panel?: WorkflowPanelState
  command?: BridgeCommand
}

export function workflowPanelFromDetail(detail: WorkflowDetailPayload): Extract<WorkflowPanelState, { mode: 'overview' }> {
  return {
    mode: 'overview',
    overview: workflowOverviewFromDetail(detail),
    detailSource: detail,
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

export function workflowPanelWithRunUpdate(panel: WorkflowPanelState, run: WorkflowRun): WorkflowPanelState {
  if (panel.mode === 'list') {
    const existing = panel.runs.filter(item => item.runId !== run.runId)
    return { ...panel, runs: [...existing, run] }
  }
  if (panel.mode === 'overview') {
    if (panel.overview.run.runId !== run.runId) return panel
    return workflowPanelFromDetail({ ...panel.detailSource, run })
  }
  if (panel.mode === 'agent_detail') {
    if (panel.overview.run.runId !== run.runId) return panel
    const overviewPanel = workflowPanelFromDetail({ ...panel.detailSource, run })
    return workflowAgentDetailPanelFromOverview(overviewPanel, panel.phaseIndex, panel.agentIndex, panel.scrollOffset)
  }
  return panel.run.runId === run.runId ? { ...panel, run } : panel
}

export function workflowPanelWithLive(
  panel: WorkflowPanelState,
  runId: string,
  jobs: WorkflowLiveJob[],
): WorkflowPanelState {
  if (panel.mode === 'list') return panel
  if (panel.mode === 'detail') {
    return panel.run.runId === runId ? { ...panel, live: mergeLiveJobs(panel.live, jobs) } : panel
  }
  if (panel.mode !== 'overview' && panel.mode !== 'agent_detail') return panel
  if (panel.overview.run.runId !== runId) return panel
  const detailSource = { ...panel.detailSource, live: mergeLiveJobs(panel.detailSource.live, jobs) }
  const selectedPhase = panel.mode === 'agent_detail' ? panel.phaseIndex : panel.overview.selectedPhase
  const overview = workflowOverviewFromDetail(detailSource, selectedPhase)
  if (panel.mode === 'overview') return { mode: 'overview', overview, detailSource }
  return workflowAgentDetailPanelFromOverview(
    { mode: 'overview', overview, detailSource },
    panel.phaseIndex,
    panel.agentIndex,
    panel.scrollOffset,
  )
}

function mergeLiveJobs(
  existing: Record<string, WorkflowLiveJob> | undefined,
  jobs: WorkflowLiveJob[],
): Record<string, WorkflowLiveJob> {
  const merged: Record<string, WorkflowLiveJob> = { ...(existing ?? {}) }
  for (const job of jobs) {
    if (job && typeof job.jobId === 'string' && job.jobId) merged[job.jobId] = job
  }
  return merged
}

export function workflowListPanelFromRuns(runs: WorkflowRun[]): WorkflowListPanelState {
  return { mode: 'list', runs, selected: 0 }
}

export function workflowListRows(panel: WorkflowListPanelState): string[] {
  const completed = panel.runs.filter(run => run.status === 'succeeded' || run.status === 'degraded').length
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
  if (status === 'degraded' || status === 'partial') return '!'
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
    const agent = overviewAgentFromProgress(
      entry,
      jobs.find(job => job.jobId === entry.jobId || job.metadata?.label === label),
      liveFor(detail.live, entry.jobId, entry.agentId, label),
    )
    if (!phases.has(phaseTitle)) phases.set(phaseTitle, [])
    phases.get(phaseTitle)!.push(agent)
  }

  if (phases.size === 0) {
    for (const job of jobs) {
      const label = jobLabel(job)
      const phaseTitle = job.phase || draftPhaseByLabel.get(label) || '未分阶段'
      if (!phases.has(phaseTitle)) phases.set(phaseTitle, [])
      phases.get(phaseTitle)!.push(overviewAgentFromJob(job, liveFor(detail.live, job.jobId, undefined, label)))
    }
  }

  if (phases.size === 0) {
    for (const phase of draft?.plan.phases ?? []) {
      const title = phase.title || '未分阶段'
      const agents = (phase.agents ?? []).map(agent => {
        const label = agent.label || 'agent'
        const live = liveFor(detail.live, label)
        return {
          id: label,
          label,
          // Live telemetry only exists while a child runs, so its presence is
          // the most accurate status a draft-only phase can report.
          status: live ? 'running' : 'registered',
          toolCount: live?.toolCalls ?? 0,
          tokenText: formatTokenUsage(live?.tokenUsage) ?? undefined,
          turnCount: live?.turn,
          elapsedText: formatElapsed(live?.elapsedSeconds),
          lastToolName: stringMetadata(live?.lastToolName),
        }
      })
      if (agents.length > 0) phases.set(title, agents)
    }
  }

  const phaseList = Array.from(phases.entries()).map(([title, agents]) => ({
    title,
    agents,
    completed: agents.filter(agent => agent.status === 'succeeded' || agent.status === 'cached' || agent.status === 'degraded').length,
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
    integrationStatus: workflowIntegrationStatus(detail),
    finalAuditStatus: workflowFinalAuditStatus(detail),
    phases: phaseList,
    selectedPhase: clampedSelected,
    completed,
    total,
  }
}

export function workflowOverviewRows(overview: WorkflowOverview): string[] {
  const selectedPhase = overview.phases[overview.selectedPhase]
  const gateSummary = [
    overview.integrationStatus ? `integration ${overview.integrationStatus}` : null,
    overview.finalAuditStatus ? `audit ${overview.finalAuditStatus}` : null,
  ].filter(Boolean).join(' · ')
  const headline = `${overview.name}  ${overview.completed}/${overview.total} agents · ${overview.status}${gateSummary ? ` · ${gateSummary}` : ''}`
  const rows = [
    headline,
    overview.description,
    `Phases | ${selectedPhase ? `${selectedPhase.title} · ${selectedPhase.total} ${selectedPhase.total === 1 ? 'agent' : 'agents'}` : 'Agents'}`,
  ]
  const maxRows = Math.max(overview.phases.length, selectedPhase?.agents.length ?? 0)
  for (let index = 0; index < maxRows; index++) {
    const phase = overview.phases[index]
    const agent = selectedPhase?.agents[index]
    rows.push(`${phase ? phaseRow(phase, index === overview.selectedPhase) : ''.padEnd(16)} | ${agent ? overviewAgentRow(agent) : ''}`)
  }
  rows.push('Enter agent · j/k phase · PgUp/PgDn · Esc back')
  return rows
}

function workflowIntegrationStatus(detail: WorkflowDetailPayload): string | null {
  return stringMetadata(detail.progress?.integrationStatus) || stringMetadata(detail.run.metadata?.integrationStatus) || null
}

function workflowFinalAuditStatus(detail: WorkflowDetailPayload): string | null {
  return stringMetadata(detail.progress?.finalAuditStatus) || stringMetadata(detail.run.metadata?.finalAuditStatus) || null
}

export function workflowAgentDetailPanelFromOverview(
  panel: WorkflowOverviewPanelState,
  phaseIndex = panel.overview.selectedPhase,
  agentIndex = 0,
  scrollOffset = 0,
): WorkflowAgentDetailPanelState {
  const phase = panel.overview.phases[Math.min(Math.max(0, phaseIndex), Math.max(0, panel.overview.phases.length - 1))]
  const agents = phase?.agents ?? []
  const clampedAgentIndex = Math.min(Math.max(0, agentIndex), Math.max(0, agents.length - 1))
  const detail = resolveAgentDetail(panel.detailSource, agents[clampedAgentIndex], phase?.title || 'Agents')
  return {
    mode: 'agent_detail',
    overview: panel.overview,
    detailSource: panel.detailSource,
    phaseIndex: panel.overview.phases.indexOf(phase!),
    agentIndex: clampedAgentIndex,
    scrollOffset: Math.max(0, scrollOffset),
    agents,
    detail,
  }
}

function resolveAgentDetail(source: WorkflowDetailPayload, agent: WorkflowOverviewAgent | undefined, phaseTitle: string): WorkflowAgentDetail {
  const jobs = source.run.jobs ?? []
  const entries = source.progress?.workflowProgress ?? []
  const job = jobs.find(candidate => candidate.jobId === agent?.id || jobLabel(candidate) === agent?.label)
  const entry = entries.find(candidate => candidate.jobId === agent?.id || candidate.agentId === agent?.id || candidate.label === agent?.label)
  const label = agent?.label || stringMetadata(entry?.label) || jobLabel(job ?? { jobId: 'agent', status: 'registered' })
  const live = liveFor(source.live, entry?.jobId, entry?.agentId, agent?.id, label, job?.jobId)
  const liveToolName = stringMetadata(live?.lastToolName)
  const toolCalls = normalizedToolCalls(entry)
  const activityRows = toolCalls.length > 0
    ? toolCalls.slice(-ACTIVITY_TAIL_ROWS)
    : liveToolName
      ? [`${liveToolName}${stringMetadata(live?.lastToolSummary) ? ` · ${stringMetadata(live?.lastToolSummary)}` : ''}`]
      : entry?.lastToolName
        ? [`${entry.lastToolName}${entry.lastToolSummary ? ` · ${entry.lastToolSummary}` : ''}`]
        : ['(no recent activity)']
  const status = agent?.status || entry?.state || job?.status || 'registered'
  const prompt = stringMetadata(job?.prompt) || stringMetadata(entry?.promptPreview) || draftAgentPrompt(source.draft ?? null, phaseTitle, label) || '(no prompt)'
  const outcome = stringMetadata(entry?.resultPreview) || stringMetadata(job?.error) || stringMetadata(entry?.error) || (status === 'running' || status === 'queued' || status === 'registered' ? '(no outcome yet)' : '(agent did not produce an outcome)')
  return {
    id: agent?.id || stringMetadata(entry?.jobId) || stringMetadata(entry?.agentId) || job?.jobId || label,
    label,
    status,
    statusText: workflowStatusText(status),
    model: agent?.model || stringMetadata(job?.metadata?.model),
    // While a child runs the durable snapshot has no token total yet; the live
    // channel is the only place that knows it.
    tokenText:
      agent?.tokenText
      || formatTokenUsage(entry?.tokenUsage)
      || formatTokenUsage(live?.tokenUsage)
      || formatTokenUsage(job?.metadata?.tokenUsage)
      || undefined,
    toolCount: toolCalls.length || live?.toolCalls || agent?.toolCount || 0,
    turnCount: live?.turn,
    elapsedText: formatElapsed(live?.elapsedSeconds),
    prompt,
    activityRows,
    activityTotal: toolCalls.length || activityRows.filter(row => row !== '(no recent activity)').length,
    outcome,
  }
}

function workflowAgentDetailRows(panel: WorkflowAgentDetailPanelState): string[] {
  const phase = panel.overview.phases[panel.phaseIndex]
  const detailRows = agentDetailRightRows(panel.detail)
  const bodyRows = detailBodyRows()
  const scroll = workflowAgentDetailScrollInfo(panel)
  const visibleDetailRows = detailRows.slice(scroll.offset, scroll.offset + bodyRows)
  const scrollLabel = scroll.maxOffset > 0 ? `  ${scroll.offset + 1}-${scroll.offset + visibleDetailRows.length}/${scroll.total}` : ''
  const rows = [`${phase?.title || 'Agents'} · ${panel.agents.length} ${panel.agents.length === 1 ? 'agent' : 'agents'} | ${panel.detail.label}${scrollLabel}`]
  // Keep the selected agent inside the visible window instead of letting the
  // list overflow the panel and hide the selection.
  const agentOffset = Math.min(Math.max(0, panel.agentIndex - bodyRows + 1), Math.max(0, panel.agents.length - bodyRows))
  for (let index = 0; index < bodyRows; index++) {
    const agentIndex = agentOffset + index
    const agent = panel.agents[agentIndex]
    const left = agent ? agentDetailAgentRow(agent, agentIndex === panel.agentIndex) : ''.padEnd(18)
    const right = visibleDetailRows[index] ?? ''
    rows.push(`${left} | ${right}`)
  }
  const hiddenAgents = panel.agents.length - bodyRows
  const agentHint = hiddenAgents > 0 ? ` · ${panel.agents.length} agents` : ''
  rows.push(`↑↓ agent · j/k scroll · PgUp/PgDn page · g/G ends · esc back${agentHint}`)
  return rows
}

function agentDetailRightRows(detail: WorkflowAgentDetail): string[] {
  const header = [
    `${workflowStatusIcon(detail.status)} ${detail.statusText}`,
    detail.turnCount ? `turn ${detail.turnCount}` : null,
    detail.elapsedText,
    detail.model,
    detail.tokenText,
    detail.toolCount ? `${detail.toolCount} ${detail.toolCount === 1 ? 'tool call' : 'tool calls'}` : null,
  ].filter(Boolean).join(' · ')
  const activityCount = detail.activityTotal
  const shown = detail.activityRows.filter(row => row !== '(no recent activity)').length
  return [
    header,
    'Prompt',
    `  ${detail.prompt}`,
    `Activity${activityCount ? ` · last ${shown} of ${activityCount} tool calls` : ''}`,
    ...detail.activityRows.map(row => `  ${row}`),
    'Outcome',
    `  ${detail.outcome}`,
  ]
}

/** Body rows available to the agent list / detail table (header + hint excluded). */
function detailBodyRows(): number {
  return Math.max(1, WORKFLOW_PANEL_MAX_ROWS - 2)
}

export function workflowAgentDetailScrollInfo(panel: WorkflowAgentDetailPanelState): { offset: number; maxOffset: number; total: number } {
  const total = agentDetailRightRows(panel.detail).length
  const maxOffset = Math.max(0, total - detailBodyRows())
  return { offset: Math.min(panel.scrollOffset, maxOffset), maxOffset, total }
}

function agentDetailAgentRow(agent: WorkflowOverviewAgent, selected: boolean): string {
  return `${selected ? '›' : ' '} ${workflowStatusIcon(agent.status)} ${agent.label}`
}

function workflowStatusText(status: string): string {
  if (status === 'succeeded' || status === 'cached') return 'Completed'
  if (status === 'degraded' || status === 'partial') return 'Degraded'
  if (status === 'running') return 'Running'
  if (status === 'queued' || status === 'registered') return 'Pending'
  if (status === 'failed') return 'Failed'
  if (status === 'cancelled' || status === 'killed') return 'Stopped'
  return status
}

function normalizedToolCalls(entry?: WorkflowProgressEntry): string[] {
  return (entry?.toolCalls ?? []).filter(tool => typeof tool === 'string' && tool.trim()).map(tool => tool.trim())
}

function draftAgentPrompt(draft: WorkflowDraftPayload | null, phaseTitle: string, label: string): string | undefined {
  for (const phase of draft?.plan.phases ?? []) {
    if ((phase.title || '未分阶段') !== phaseTitle) continue
    const agent = (phase.agents ?? []).find(candidate => candidate.label === label)
    if (agent) return stringMetadata(agent.prompt)
  }
  return undefined
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

function liveFor(
  live: Record<string, WorkflowLiveJob> | undefined,
  ...keys: Array<string | null | undefined>
): WorkflowLiveJob | undefined {
  if (!live) return undefined
  for (const key of keys) {
    if (typeof key === 'string' && key && live[key]) return live[key]
  }
  return undefined
}

function overviewAgentFromProgress(entry: WorkflowProgressEntry, job?: WorkflowJob, live?: WorkflowLiveJob): WorkflowOverviewAgent {
  const label = stringMetadata(entry.label) || stringMetadata(job?.metadata?.label) || stringMetadata(entry.jobId) || stringMetadata(entry.agentId) || 'agent'
  return {
    id: stringMetadata(entry.jobId) || stringMetadata(entry.agentId) || job?.jobId || label,
    label,
    status: entry.state || job?.status || 'registered',
    model: stringMetadata(job?.metadata?.model),
    // While a child runs the durable snapshot has no token total yet; the live
    // channel is the only place that knows it.
    tokenText: formatTokenUsage(entry.tokenUsage) ?? formatTokenUsage(live?.tokenUsage) ?? undefined,
    toolCount: entry.toolCalls?.length ?? live?.toolCalls ?? 0,
    turnCount: live?.turn,
    elapsedText: formatElapsed(live?.elapsedSeconds),
    lastToolName: stringMetadata(live?.lastToolName),
  }
}

function overviewAgentFromJob(job: WorkflowJob, live?: WorkflowLiveJob): WorkflowOverviewAgent {
  return {
    id: job.jobId,
    label: jobLabel(job),
    status: job.status,
    model: stringMetadata(job.metadata?.model),
    tokenText: formatTokenUsage(job.metadata?.tokenUsage) ?? formatTokenUsage(live?.tokenUsage) ?? undefined,
    toolCount: Array.isArray(job.metadata?.toolCalls) ? job.metadata.toolCalls.length : (live?.toolCalls ?? 0),
    turnCount: live?.turn,
    elapsedText: formatElapsed(live?.elapsedSeconds),
    lastToolName: stringMetadata(live?.lastToolName),
  }
}

function formatElapsed(seconds: unknown): string | undefined {
  const value = numberValue(seconds)
  if (value === null || value < 0) return undefined
  if (value < 60) return `${Math.round(value)}s`
  const minutes = Math.floor(value / 60)
  const rest = Math.round(value - minutes * 60)
  return `${minutes}m ${rest}s`
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
  const live = agent.turnCount && agent.status === 'running' ? `turn ${agent.turnCount}` : null
  const stats = [
    live,
    agent.elapsedText,
    agent.tokenText,
    agent.toolCount ? `${agent.toolCount} tools` : null,
    agent.lastToolName,
  ].filter(Boolean).join(' · ')
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
  if (panel.mode === 'agent_detail') return workflowAgentDetailRows(panel)
  const permission = panel.run.permissionProfile || '(default)'
  const jobs = panel.run.jobs?.length ?? 0
  const scriptLines = panel.script.split('\n')
  const controls = resumeableWorkflowStatuses.has(panel.run.status)
    ? 'r resume - s stop - Esc close'
    : panel.run.status === 'running'
      ? 's stop - Esc close'
      : 'Esc close'
  return [
    `Workflow ${panel.run.runId} - ${panel.run.status}`,
    `Permission: ${permission}`,
    `Jobs: ${jobs}`,
    'Script:',
    ...(scriptLines.length > 0 ? scriptLines : ['']),
    controls,
  ]
}

const resumeableWorkflowStatuses = new Set(['failed', 'killed', 'interrupted', 'succeeded', 'degraded'])

export function workflowPanelCommandForKey(
  panel: WorkflowPanelState,
  key: InputKey,
  rawInput: string,
): WorkflowPanelDecision | null {
  if (panel.mode === 'list') return workflowListCommandForKey(panel, key, rawInput)
  const lowered = rawInput.toLowerCase()
  if (panel.mode === 'overview') {
    if (key.upArrow || lowered === 'k') return { panel: overviewPanelWithPhase(panel, panel.overview.selectedPhase - 1) }
    if (key.downArrow || lowered === 'j') return { panel: overviewPanelWithPhase(panel, panel.overview.selectedPhase + 1) }
    if (key.pageUp) return { panel: overviewPanelWithPhase(panel, panel.overview.selectedPhase - 5) }
    if (key.pageDown) return { panel: overviewPanelWithPhase(panel, panel.overview.selectedPhase + 5) }
    if (rawInput === 'g') return { panel: overviewPanelWithPhase(panel, 0) }
    if (rawInput === 'G') return { panel: overviewPanelWithPhase(panel, panel.overview.phases.length - 1) }
    if (key.return) {
      const phase = panel.overview.phases[panel.overview.selectedPhase]
      return phase && phase.agents.length > 0 ? { panel: workflowAgentDetailPanelFromOverview(panel) } : null
    }
    return workflowRunControlForKey(panel.overview.run, key, rawInput)
  }
  if (panel.mode === 'agent_detail') {
    if (key.escape) {
      return { panel: { mode: 'overview', overview: { ...panel.overview, selectedPhase: panel.phaseIndex }, detailSource: panel.detailSource } }
    }
    if (key.upArrow) return { panel: workflowAgentDetailPanelFromOverview({ mode: 'overview', overview: panel.overview, detailSource: panel.detailSource }, panel.phaseIndex, Math.max(0, panel.agentIndex - 1), panel.scrollOffset) }
    if (key.downArrow) return { panel: workflowAgentDetailPanelFromOverview({ mode: 'overview', overview: panel.overview, detailSource: panel.detailSource }, panel.phaseIndex, Math.min(Math.max(0, panel.agents.length - 1), panel.agentIndex + 1), panel.scrollOffset) }
    const page = detailBodyRows()
    if (lowered === 'j') return { panel: detailPanelWithScroll(panel, panel.scrollOffset + 1) }
    if (lowered === 'k') return { panel: detailPanelWithScroll(panel, panel.scrollOffset - 1) }
    if (key.pageDown) return { panel: detailPanelWithScroll(panel, panel.scrollOffset + page) }
    if (key.pageUp) return { panel: detailPanelWithScroll(panel, panel.scrollOffset - page) }
    if (rawInput === 'g') return { panel: detailPanelWithScroll(panel, 0) }
    if (rawInput === 'G') return { panel: detailPanelWithScroll(panel, Number.MAX_SAFE_INTEGER) }
    return workflowRunControlForKey(panel.overview.run, key, rawInput)
  }
  return workflowRunControlForKey(panel.run, key, rawInput)
}

/** Keys the workflow overlay owns while it is open. */
const WORKFLOW_PANEL_LETTER_KEYS = new Set(['j', 'k', 'g', 'x', 'r', 's'])

/**
 * True when an open workflow panel should consume this key.
 *
 * Without this, a key the panel does not act on (`j` in the overview, for
 * example) fell through to the composer and was typed as text -- which then
 * made the status-bar shortcut stop working, because that path only runs while
 * the composer is empty.
 */
export function workflowPanelCapturesKey(
  panel: WorkflowPanelState,
  key: InputKey,
  rawInput: string,
): boolean {
  if (key.escape || key.return || key.upArrow || key.downArrow || key.pageUp || key.pageDown) return true
  if (key.ctrl || key.meta) return false
  if (panel.mode === 'list') return false
  return WORKFLOW_PANEL_LETTER_KEYS.has(rawInput)
}

function overviewPanelWithPhase(panel: WorkflowOverviewPanelState, selectedPhase: number): WorkflowPanelState {
  const clamped = Math.min(Math.max(0, selectedPhase), Math.max(0, panel.overview.phases.length - 1))
  return { mode: 'overview', overview: { ...panel.overview, selectedPhase: clamped }, detailSource: panel.detailSource }
}

function detailPanelWithScroll(panel: WorkflowAgentDetailPanelState, scrollOffset: number): WorkflowPanelState {
  // Clamp against the rendered window so holding `j` cannot build an offset
  // that then needs the same number of `k` presses to undo.
  const maxOffset = workflowAgentDetailScrollInfo(panel).maxOffset
  return workflowAgentDetailPanelFromOverview(
    { mode: 'overview', overview: panel.overview, detailSource: panel.detailSource },
    panel.phaseIndex,
    panel.agentIndex,
    Math.min(Math.max(0, scrollOffset), maxOffset),
  )
}

function workflowRunControlForKey(run: WorkflowRun, key: InputKey, rawInput: string): WorkflowPanelDecision | null {
  if (key.ctrl || key.meta) return null
  if (rawInput === 'x' || rawInput === 's') {
    if (run.status !== 'running') return null
    return { command: { type: 'workflow_stop', runId: run.runId, reason: 'stopped from Ink UI' } }
  }
  if (rawInput === 'r' && resumeableWorkflowStatuses.has(run.status)) {
    return { command: { type: 'workflow_resume', runId: run.runId } }
  }
  return null
}
