/**
 * Read-only goal status card model (Stage F phase 5, S1).
 *
 * Mirrors CC's "Goal active" card (m2146:200-246): a passive, non-focusable
 * strip. Kept as pure functions so the exact copy is unit-testable without
 * rendering Ink.
 */

export type GoalMetadata = {
  condition: string
  iterations: number
  set_at: number
  tokens_at_start: number
  tokens_spent: number
  met: boolean
  last_reason?: string
}

export type GoalTerminal = {
  met: boolean
  failed?: boolean
  condition?: string
  iterations?: number
  tokens?: number
  durationMs?: number
}

export function formatGoalDuration(ms: number): string {
  const safe = Math.max(0, Math.floor(ms / 1000))
  if (safe < 60) return `${safe}s`
  const minutes = Math.floor(safe / 60)
  const rest = safe % 60
  if (minutes < 60) return `${minutes}m ${rest}s`
  const hours = Math.floor(minutes / 60)
  return `${hours}h ${minutes % 60}m`
}

export function formatGoalTokens(tokens: number): string {
  const safe = Math.max(0, Math.round(tokens))
  if (safe < 1000) return String(safe)
  const value = safe / 1000
  return `${Number.isInteger(value) ? String(value) : value.toFixed(1)}k`
}

/** Live elapsed time for an active goal, in milliseconds. */
export function goalElapsedMs(meta: GoalMetadata, nowMs: number): number {
  const setAtMs = Number(meta.set_at) * 1000
  if (!Number.isFinite(setAtMs) || setAtMs <= 0) return 0
  return Math.max(0, Math.floor(nowMs - setAtMs))
}

/**
 * Lines for the active goal card. Empty when there is no goal, so the caller
 * simply renders nothing and the layout reserves zero rows.
 */
export function goalCardLines(meta: GoalMetadata | null, nowMs: number): string[] {
  if (!meta) return []
  const turns = Math.max(0, Math.floor(meta.iterations || 0))
  const parts = [
    `running ${formatGoalDuration(goalElapsedMs(meta, nowMs))}`,
    `${turns} ${turns === 1 ? 'turn' : 'turns'}`,
    `${formatGoalTokens(meta.tokens_spent)} tokens`,
  ]
  const line1 = `◎ Goal active · ${parts.join(' · ')}`
  const goal = condense(meta.condition)
  let line2 = `Goal: ${goal}`
  const last = condense(meta.last_reason || '')
  if (last) line2 += `  ·  Last check: ${last}`
  return [line1, line2]
}

/** A one-line terminal badge shown after a goal is met/failed (sentinel hidden). */
export function goalTerminalLines(terminal: GoalTerminal | null): string[] {
  if (!terminal) return []
  const icon = terminal.met ? '✓' : '✗'
  const label = terminal.met ? 'Goal met' : 'Goal failed'
  const extras: string[] = []
  if (typeof terminal.durationMs === 'number') extras.push(formatGoalDuration(terminal.durationMs))
  if (typeof terminal.iterations === 'number') {
    const t = terminal.iterations
    extras.push(`${t} ${t === 1 ? 'turn' : 'turns'}`)
  }
  if (typeof terminal.tokens === 'number') extras.push(`${formatGoalTokens(terminal.tokens)} tokens`)
  const suffix = extras.length ? ` · ${extras.join(' · ')}` : ''
  return [`${icon} ${label}${suffix}`]
}

function condense(text: string): string {
  return String(text || '').replace(/\s+/g, ' ').trim()
}
