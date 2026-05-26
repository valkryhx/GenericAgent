import type { BridgeCommand } from './protocol.js'
import { compactPasteRefs, appendFoldedText, expandPastedTextRefs, flushPendingPaste, type PasteStore } from './paste.js'

export type InputKey = {
  ctrl?: boolean
  meta?: boolean
  shift?: boolean
  return?: boolean
  tab?: boolean
  backspace?: boolean
  delete?: boolean
  escape?: boolean
  upArrow?: boolean
  downArrow?: boolean
  leftArrow?: boolean
  rightArrow?: boolean
  pageUp?: boolean
  pageDown?: boolean
  sequence?: string
}

export type InputStatus = 'connecting' | 'idle' | 'running' | 'stopping'

export type InputDecision = {
  value: string
  cursorOffset?: number
  command?: BridgeCommand
  action?: { type: 'open_resume' | 'open_rewind' | 'open_mcp' | 'open_model' | 'open_theme' | 'clear' | 'help' | 'status' }
  exit?: boolean
}

function clampCursorOffset(value: string, cursorOffset: number): number {
  return Math.max(0, Math.min(value.length, Math.floor(cursorOffset)))
}

function makeDecision(
  value: string,
  cursorOffset: number,
  includeCursorOffset: boolean,
  extra: Omit<InputDecision, 'value' | 'cursorOffset'> = {},
): InputDecision {
  const decision: InputDecision = { value, ...extra }
  if (includeCursorOffset) decision.cursorOffset = clampCursorOffset(value, cursorOffset)
  return decision
}

function insertFoldedTextAtCursor(value: string, cursorOffset: number, text: string, pasteStore: PasteStore) {
  const before = value.slice(0, cursorOffset)
  const after = value.slice(cursorOffset)
  const insertedPrefix = appendFoldedText(before, text, pasteStore)
  return {
    value: insertedPrefix + after,
    cursorOffset: insertedPrefix.length,
  }
}

function insertLiteralTextAtCursor(value: string, cursorOffset: number, text: string) {
  return {
    value: value.slice(0, cursorOffset) + text + value.slice(cursorOffset),
    cursorOffset: cursorOffset + text.length,
  }
}

function parseSlashSubmit(
  text: string,
  skillNames: ReadonlySet<string> = new Set(),
): Pick<InputDecision, 'command' | 'action' | 'exit'> | null {
  const trimmed = text.trim()
  const indexedResume = /^\/(?:resume|continue)\s+(\d+)$/.exec(trimmed)
  if (indexedResume) {
    return { command: { type: 'resume_session_index', index: Number(indexedResume[1]) } }
  }
  if (/^\/(?:resume|continue)$/.test(trimmed)) return { action: { type: 'open_resume' } }
  if (/^\/(?:rewind|checkpoint)$/.test(trimmed)) return { action: { type: 'open_rewind' } }
  if (trimmed === '/mcp') return { action: { type: 'open_mcp' } }
  const mcpAction = /^\/mcp\s+(reconnect|enable|disable)\s+(.+)$/.exec(trimmed)
  if (mcpAction) {
    const server = mcpAction[2]
    if (mcpAction[1] === 'reconnect') return { command: { type: 'mcp_reconnect', server } }
    if (mcpAction[1] === 'enable') return { command: { type: 'mcp_enable', server } }
    return { command: { type: 'mcp_disable', server } }
  }
  if (/^\/(?:model|llm)$/.test(trimmed)) return { action: { type: 'open_model' } }
  if (trimmed === '/theme') return { action: { type: 'open_theme' } }
  const modelSwitch = /^\/(?:model|llm)\s+(.+)$/.exec(trimmed)
  if (modelSwitch) {
    const selector = modelSwitch[1].trim()
    if (selector === '?' || selector.toLowerCase() === 'help') return { command: { type: 'model_status' } }
    return { command: { type: 'model_switch', selector } }
  }
  const compact = /^\/compact(?:\s+([\s\S]*))?$/.exec(trimmed)
  if (compact) return { command: { type: 'compact', instructions: compact[1]?.trim() ?? '' } }
  if (trimmed === '/new' || trimmed === '/reset') return { command: { type: 'new_session' } }
  if (trimmed === '/stop') return { command: { type: 'stop' } }
  if (trimmed === '/clear') return { action: { type: 'clear' } }
  if (trimmed === '/help') return { action: { type: 'help' } }
  if (trimmed === '/status') return { action: { type: 'status' } }
  if (trimmed === '/quit' || trimmed === '/exit') return { command: { type: 'shutdown' }, exit: true }
  const skillMatch = /^\/([^\s/]+)(?:\s+([\s\S]*))?$/.exec(trimmed)
  if (skillMatch) {
    const skill = skillMatch[1]
    if (skillNames.has(skill)) return { command: { type: 'skill_invoke', skill, args: skillMatch[2] ?? '' } }
  }
  return null
}

export function handleInput(
  value: string,
  rawInput: string,
  key: InputKey,
  status: InputStatus,
  pasteStore: PasteStore,
  skillNames: ReadonlySet<string> = new Set(),
  cursorOffset?: number,
): InputDecision {
  const includeCursorOffset = cursorOffset !== undefined
  const offset = clampCursorOffset(value, cursorOffset ?? value.length)
  const decision = (
    nextValue: string,
    nextCursorOffset = nextValue.length,
    extra: Omit<InputDecision, 'value' | 'cursorOffset'> = {},
  ) => makeDecision(nextValue, nextCursorOffset, includeCursorOffset, extra)

  if (key.ctrl && rawInput === 'c') {
    return decision(value, offset, { command: { type: 'shutdown' }, exit: true })
  }
  if (key.escape) {
    return status === 'running' || status === 'stopping'
      ? decision(value, offset, { command: { type: 'stop' } })
      : decision(value, offset)
  }
  if (key.leftArrow) {
    return decision(value, offset - 1)
  }
  if (key.rightArrow) {
    return decision(value, offset + 1)
  }
  if ((key.meta || key.shift) && key.return) {
    const inserted = insertLiteralTextAtCursor(value, offset, '\n')
    return decision(inserted.value, inserted.cursorOffset)
  }
  if (!key.return && (rawInput === '\r' || rawInput === '\n')) {
    const inserted = insertLiteralTextAtCursor(value, offset, '\n')
    return decision(inserted.value, inserted.cursorOffset)
  }
  if (key.ctrl && rawInput === 'j') {
    const inserted = insertLiteralTextAtCursor(value, offset, '\n')
    return decision(inserted.value, inserted.cursorOffset)
  }
  if (key.backspace) {
    if (offset <= 0) return decision(value, offset)
    return decision(value.slice(0, offset - 1) + value.slice(offset), offset - 1)
  }
  if (key.delete && (key.sequence === '\x7f' || key.sequence === '\b')) {
    if (offset <= 0) return decision(value, offset)
    return decision(value.slice(0, offset - 1) + value.slice(offset), offset - 1)
  }
  if (key.delete) {
    if (offset >= value.length) return decision(value, offset)
    return decision(value.slice(0, offset) + value.slice(offset + 1), offset)
  }
  if (key.return) {
    if (value.endsWith('\\') && (!includeCursorOffset || offset === value.length)) {
      const nextValue = `${value.slice(0, -1)}\n`
      return decision(nextValue, nextValue.length)
    }
    const prepared = flushPendingPaste(compactPasteRefs(value, pasteStore), pasteStore)
    const expanded = expandPastedTextRefs(prepared, pasteStore).trimEnd()
    if (!expanded) return decision(value, offset)
    const slash = parseSlashSubmit(expanded, skillNames)
    if (slash) return decision('', 0, slash)
    if (status === 'running' || status === 'stopping' || status === 'connecting') return decision(value, offset)
    return decision('', 0, { command: { type: 'submit', text: expanded } })
  }
  if (rawInput) {
    const inserted = insertFoldedTextAtCursor(value, offset, rawInput, pasteStore)
    return decision(inserted.value, inserted.cursorOffset)
  }
  return decision(value, offset)
}
