import type { BridgeCommand } from './protocol.js'
import { expandPastedTextRefs, foldPastedText, type PasteStore } from './paste.js'

export type InputKey = {
  ctrl?: boolean
  meta?: boolean
  shift?: boolean
  return?: boolean
  backspace?: boolean
  delete?: boolean
  escape?: boolean
}

export type InputStatus = 'connecting' | 'idle' | 'running' | 'stopping'

export type InputDecision = {
  value: string
  command?: BridgeCommand
  action?: { type: 'open_resume' | 'open_rewind' | 'clear' | 'help' | 'status' }
  exit?: boolean
}

function parseSlashSubmit(text: string): Pick<InputDecision, 'command' | 'action' | 'exit'> | null {
  const trimmed = text.trim()
  const indexedResume = /^\/(?:resume|continue)\s+(\d+)$/.exec(trimmed)
  if (indexedResume) {
    return { command: { type: 'resume_session_index', index: Number(indexedResume[1]) } }
  }
  if (/^\/(?:resume|continue)$/.test(trimmed)) return { action: { type: 'open_resume' } }
  if (/^\/(?:rewind|checkpoint)$/.test(trimmed)) return { action: { type: 'open_rewind' } }
  if (trimmed === '/stop') return { command: { type: 'stop' } }
  if (trimmed === '/clear') return { action: { type: 'clear' } }
  if (trimmed === '/help') return { action: { type: 'help' } }
  if (trimmed === '/status') return { action: { type: 'status' } }
  if (trimmed === '/quit' || trimmed === '/exit') return { command: { type: 'shutdown' }, exit: true }
  return null
}

export function handleInput(
  value: string,
  rawInput: string,
  key: InputKey,
  status: InputStatus,
  pasteStore: PasteStore,
): InputDecision {
  if (key.ctrl && rawInput === 'c') {
    return { value, command: { type: 'shutdown' }, exit: true }
  }
  if (key.escape) {
    return status === 'running' || status === 'stopping' ? { value, command: { type: 'stop' } } : { value }
  }
  if ((key.meta || key.shift) && key.return) {
    return { value: `${value}\n` }
  }
  if (!key.return && (rawInput === '\r' || rawInput === '\n')) {
    return { value: `${value}\n` }
  }
  if (key.ctrl && rawInput === 'j') {
    return { value: `${value}\n` }
  }
  if (key.backspace || key.delete) {
    return { value: value.slice(0, -1) }
  }
  if (key.return) {
    const expanded = expandPastedTextRefs(value, pasteStore).trimEnd()
    if (!expanded) return { value }
    const slash = parseSlashSubmit(expanded)
    if (slash) return { value: '', ...slash }
    if (status === 'running' || status === 'stopping' || status === 'connecting') return { value }
    return { value: '', command: { type: 'submit', text: expanded } }
  }
  if (rawInput) {
    return { value: value + foldPastedText(rawInput, pasteStore) }
  }
  return { value }
}
