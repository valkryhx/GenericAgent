import type { BridgeCommand, TeamMember, TeamRoster, TeamTask } from './protocol.js'
import type { InputKey } from './inputController.js'

/**
 * Execution-time team panel (GA ``/team`` / ``/agents``).
 *
 * CC renders a running swarm as tmux panes (``m1573`` ``swarm-view``: pane title,
 * border colour, rebalanced layout) and ships no in-TUI roster list in this build.
 * GA has no tmux multiplexing, so the faithful analogue is the same overlay the
 * ``/workflows`` command already uses: a live list of teammates plus the shared
 * task board (CC ``TaskList``). The panel is a pure function of the roster event
 * so it can be unit-tested without Ink.
 */
export const TEAM_PANEL_MAX_ROWS = 16

export type TeamPanelState = {
  roster: TeamRoster
  members: TeamMember[]
  tasks: TeamTask[]
  selected: number
}

export function teamPanelFromRoster(event: {
  team: TeamRoster
  members?: TeamMember[]
  tasks?: TeamTask[]
}): TeamPanelState {
  return {
    roster: event.team,
    members: Array.isArray(event.members) ? event.members : [],
    tasks: Array.isArray(event.tasks) ? event.tasks : [],
    selected: 0,
  }
}

const MEMBER_GLYPH: Record<string, string> = {
  running: '\u25cf',
  idle: '\u25cb',
  closed: '\u00b7',
  failed: '\u2717',
  evicted: '\u00d7',
}

const TASK_GLYPH: Record<string, string> = {
  completed: '[x]',
  in_progress: '[~]',
  pending: '[ ]',
}

export function teamMemberGlyph(status: string | null | undefined): string {
  return MEMBER_GLYPH[String(status || '').toLowerCase()] ?? '\u25cb'
}

export function teamTaskGlyph(status: string | null | undefined): string {
  return TASK_GLYPH[String(status || '').toLowerCase()] ?? '[ ]'
}

function padEnd(value: string, width: number): string {
  return value.length >= width ? value : value + ' '.repeat(width - value.length)
}

export function teamPanelLines(state: TeamPanelState): string[] {
  const roster = state.roster
  const lines: string[] = []
  lines.push(
    `Team ${roster.teamName} \u00b7 lead ${roster.leadName} \u00b7 ${state.members.length} member(s) \u00b7 ${state.tasks.length} task(s)`,
  )
  if (state.members.length === 0) {
    lines.push('  (no teammates yet)')
  }
  const width = Math.max(12, ...state.members.map(member => String(member.name || member.agentId || '').length))
  state.members.forEach((member, index) => {
    const marker = index === state.selected ? '\u25b8' : ' '
    const name = padEnd(String(member.name || member.agentId || ''), width)
    const status = padEnd(String(member.status || ''), 8)
    const owned = Number(member.taskCount || 0)
    const current = member.currentTask ? ` \u2192 ${member.currentTask}` : ''
    lines.push(`${marker} ${teamMemberGlyph(member.status)} ${name} ${status} ${owned} task(s)${current}`)
  })
  lines.push('')
  lines.push('Task board')
  if (state.tasks.length === 0) {
    lines.push('  (no tasks)')
  }
  for (const task of state.tasks) {
    const owner = task.owner ? String(task.owner).replace('/root/', '') : '-'
    const blocked = (task.blockedBy || []).length > 0 ? ` (blocked by ${(task.blockedBy || []).join(', ')})` : ''
    lines.push(`  ${teamTaskGlyph(task.status)} #${task.id} ${task.subject}  owner=${owner}${blocked}`)
  }
  lines.push('')
  lines.push('Up/Down select \u00b7 r refresh \u00b7 Esc close')
  return lines
}

export function moveTeamSelection(state: TeamPanelState, delta: number): TeamPanelState {
  if (state.members.length === 0) return state
  const next = (state.selected + delta + state.members.length) % state.members.length
  return { ...state, selected: next }
}

export function teamPanelCommandForKey(
  state: TeamPanelState,
  key: InputKey,
  rawInput: string,
): { panel?: TeamPanelState; command?: BridgeCommand } | null {
  if (key.upArrow) return { panel: moveTeamSelection(state, -1) }
  if (key.downArrow) return { panel: moveTeamSelection(state, 1) }
  if (rawInput === 'r' || rawInput === 'R') return { command: { type: 'team_list' } }
  return null
}

export function teamPanelCapturesKey(state: TeamPanelState, key: InputKey, rawInput: string): boolean {
  return Boolean(key.upArrow || key.downArrow || rawInput === 'r' || rawInput === 'R')
}
