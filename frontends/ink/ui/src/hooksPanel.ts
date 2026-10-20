import type { BridgeCommand } from './protocol.js'
import type { InputKey } from './inputController.js'

/** Read-only execution-time hook inventory, modeled on CC's /hooks surface. */
export const HOOKS_PANEL_MAX_ROWS = 22

export type HookConfiguredEntry = {
  event: string
  matcher: string
  type: string
  source: string
  path: string
  statusMessage?: string
  timeout?: number
}

export type HookAuditEntry = {
  hook_run_id?: string
  event?: string
  source?: string
  handler_type?: string
  status?: string
  duration_ms?: number
  error?: string
}

export type HooksStatusPayload = {
  files: string[]
  entries: number
  handlers: number
  handlerTypes: string[]
  configured: HookConfiguredEntry[]
  events: Record<string, string>
  issues: Array<{ source: string; path: string; message: string }>
  recent: HookAuditEntry[]
}

export type HooksPanelState = {
  payload: HooksStatusPayload
  selected: number
}

export function hooksPanelFromStatus(event: HooksStatusPayload): HooksPanelState {
  return {
    payload: {
      files: Array.isArray(event.files) ? event.files : [],
      entries: Number(event.entries || 0),
      handlers: Number(event.handlers || 0),
      handlerTypes: Array.isArray(event.handlerTypes) ? event.handlerTypes : [],
      configured: Array.isArray(event.configured) ? event.configured : [],
      events: event.events && typeof event.events === 'object' ? event.events : {},
      issues: Array.isArray(event.issues) ? event.issues : [],
      recent: Array.isArray(event.recent) ? event.recent : [],
    },
    selected: 0,
  }
}

function padEnd(value: string, width: number): string {
  return value.length >= width ? value : value + ' '.repeat(width - value.length)
}

function shortPath(value: string, max = 48): string {
  const normalized = String(value || '')
  if (normalized.length <= max) return normalized
  return '...' + normalized.slice(-(max - 3))
}

export function hooksPanelLines(state: HooksPanelState): string[] {
  const p = state.payload
  const lines: string[] = []
  lines.push(`Hooks  ${p.handlers} handler(s)  ${p.entries} entr${p.entries === 1 ? 'y' : 'ies'}  [${p.handlerTypes.join(', ') || 'none'}]`)
  lines.push('')
  lines.push('Configuration')
  if (p.files.length === 0) lines.push('  (no .genericagent hook files loaded)')
  for (const file of p.files) lines.push(`  ${shortPath(file)}`)
  lines.push('')
  lines.push('Configured handlers')
  if (p.configured.length === 0) {
    lines.push('  (none)')
  }
  const eventWidth = Math.max(6, ...p.configured.map(item => item.event.length))
  const typeWidth = Math.max(8, ...p.configured.map(item => item.type.length))
  p.configured.forEach((item, index) => {
    const marker = index === state.selected ? '\u25b8' : ' '
    const matcher = item.matcher ? ` matcher=${item.matcher}` : ''
    lines.push(`${marker} ${padEnd(item.event, eventWidth)}  ${padEnd(item.type, typeWidth)}  ${item.source}${matcher}`)
  })
  lines.push('')
  lines.push('Event integration')
  const integrated = Object.entries(p.events).filter(([, status]) => status === 'integrated')
  const contractOnly = Object.entries(p.events).filter(([, status]) => status !== 'integrated')
  lines.push(`  integrated (${integrated.length}): ${integrated.map(([name]) => name).join(', ') || '-'}`)
  lines.push(`  contract-only (${contractOnly.length}): ${contractOnly.map(([name]) => name).join(', ') || '-'}`)
  if (p.issues.length > 0) {
    lines.push('')
    lines.push('Configuration issues')
    for (const issue of p.issues.slice(0, 6)) {
      lines.push(`  ! ${issue.message}  ${shortPath(issue.path, 36)}`)
    }
  }
  lines.push('')
  lines.push('Recent executions')
  if (p.recent.length === 0) lines.push('  (none)')
  for (const row of p.recent.slice().reverse().slice(0, 8)) {
    const status = String(row.status || 'unknown')
    const duration = typeof row.duration_ms === 'number' ? `${row.duration_ms}ms` : ''
    const error = row.error ? `  ${String(row.error).slice(0, 80)}` : ''
    lines.push(`  ${status.padEnd(9)} ${String(row.event || '').padEnd(18)} ${String(row.handler_type || '').padEnd(9)} ${duration}${error}`)
  }
  lines.push('')
  lines.push('r refresh \u00b7 Up/Down select \u00b7 Esc close')
  return lines
}

export function moveHooksSelection(state: HooksPanelState, delta: number): HooksPanelState {
  if (state.payload.configured.length === 0) return state
  const next = (state.selected + delta + state.payload.configured.length) % state.payload.configured.length
  return { ...state, selected: next }
}

export function hooksPanelCommandForKey(
  state: HooksPanelState,
  key: InputKey,
  rawInput: string,
): { panel?: HooksPanelState; command?: BridgeCommand } | null {
  if (key.upArrow) return { panel: moveHooksSelection(state, -1) }
  if (key.downArrow) return { panel: moveHooksSelection(state, 1) }
  if (rawInput === 'r' || rawInput === 'R') return { command: { type: 'hooks_status' } }
  return null
}

export function hooksPanelCapturesKey(state: HooksPanelState, key: InputKey, rawInput: string): boolean {
  return Boolean(key.upArrow || key.downArrow || rawInput === 'r' || rawInput === 'R')
}
