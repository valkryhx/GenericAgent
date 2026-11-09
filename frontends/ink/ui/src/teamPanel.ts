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

/**
 * Member glyphs, keyed by the *verdict* (see `teamMemberVerdict`), not by the raw registry
 * status.
 *
 * A finished member's registry row says `closed`, which is a lifecycle word: it says the
 * process was reclaimed, not whether the work succeeded. Rendering `closed` as a dot made
 * three finished teammates look like three dead ends. The verdict carries the answer, so
 * the glyph table is keyed on that instead.
 */
const VERDICT_GLYPH: Record<string, string> = {
  running: '\u25cf',
  idle: '\u25cb',
  completed: '\u2713',
  succeeded: '\u2713',
  failed: '\u2717',
  errored: '\u2717',
  interrupted: '\u2717',
  evicted: '\u00d7',
  released: '\u00b7',
  // A `running` row whose process is gone. Not a lifecycle word the registry writes --
  // it is what the panel concludes after asking the registry for liveness.
  stale: '\u00d7',
}

/** The single glyph shown when the verdict is not recognised (see below). */
export const UNKNOWN_MEMBER_GLYPH = '?'

const TASK_GLYPH: Record<string, string> = {
  completed: '[x]',
  in_progress: '[~]',
  pending: '[ ]',
}

/** Close reasons that mean the member failed before producing any work. */
const FAILED_CLOSE_STATUSES = new Set(['worktree_error', 'killed'])

/** The terminal work states a closed row can hand back through `previousStatus`. */
const WORK_VERDICTS = new Set(['completed', 'errored', 'interrupted', 'failed', 'cancelled', 'canceled'])

export type TeamMemberFields = Pick<TeamMember, 'status' | 'previousStatus' | 'closedStatus' | 'turnStatus' | 'live'>

/**
 * How a member ended, as opposed to whether its row is still open.
 *
 * `status === 'closed'` on every finished member, whatever it actually did, so the raw
 * status cannot be shown as the verdict. The registry keeps the pre-close state in
 * `previousStatus` -- that is the work verdict (`completed` / `errored` / `interrupted`).
 * For a live member the status *is* the verdict, because nothing has been concluded yet.
 *
 * A row closed *before* it ever ran has no work verdict at all (`previousStatus` is
 * `starting`). Then `closedStatus` is the only thing that knows anything: a
 * `worktree_error` means the member never got off the ground, which is a failure and must
 * not be dressed up as unknown -- nor as success.
 */
export function teamMemberVerdict(member: TeamMemberFields): string {
  const status = String(member.status || '').toLowerCase()
  const previous = String(member.previousStatus || '').toLowerCase()
  const closed = String(member.closedStatus || '').toLowerCase()
  const turn = String(member.turnStatus || '').toLowerCase()
  // A live teammate is `running` between turns, so the status alone cannot tell a
  // member that never started from one that already finished. The turn status carries
  // the real answer; `running`/`pending` mid-turn values are not verdicts.
  if (status !== 'closed') {
    if (WORK_VERDICTS.has(turn)) return turn
    // `live === false` means the registry proved the process is gone: the row is a
    // leftover, not a member at work. Unknown liveness stays `running` -- never dead.
    return member.live === false ? 'stale' : status
  }
  if (WORK_VERDICTS.has(previous)) return previous
  if (FAILED_CLOSE_STATUSES.has(closed)) return 'failed'
  // Legacy rows were closed before the registry recorded `previousStatus`
  // (`closed_status=released` writes `previous_status="running"`); the turn status is
  // then the only surviving work verdict.
  if (WORK_VERDICTS.has(turn)) return turn
  // No work verdict survived, but the close reason still says something stronger than
  // the lifecycle word `running`: a reaped row was dead, a released row handed its slot
  // back. Reporting `running` here is what painted dead members as working.
  if (closed === 'stale' || closed === 'released') return closed
  return previous || status
}

/**
 * The glyph for a member.
 *
 * An unrecognised verdict renders as `?` rather than a hollow circle. Falling back to a
 * plausible-looking glyph is how a member that finished silently reads as idle -- the
 * panel would be confidently wrong instead of visibly unsure. This is the same rule the
 * subagent announcements follow: never let an unknown word impersonate a known one.
 */
export function teamMemberGlyph(member: TeamMemberFields): string {
  return VERDICT_GLYPH[teamMemberVerdict(member)] ?? UNKNOWN_MEMBER_GLYPH
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
    // The verdict, not the lifecycle word: every finished member's row says `closed`,
    // which tells the user nothing about whether the work succeeded.
    const verdict = teamMemberVerdict(member)
    const status = padEnd(verdict, 12)
    const owned = Number(member.taskCount || 0)
    const current = member.currentTask ? ` \u2192 ${member.currentTask}` : ''
    lines.push(`${marker} ${teamMemberGlyph(member)} ${name} ${status} ${owned} task(s)${current}`)
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
