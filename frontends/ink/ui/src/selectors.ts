import type { BridgeCommand, ChatMessage, ResumeSession } from './protocol.js'

export type SelectorMode = 'resume' | 'rewind'

export type RewindOption = {
  taskId: number
  text: string
}

export type SelectorState =
  | { mode: 'resume'; selected: number; sessions: ResumeSession[]; loading?: boolean }
  | { mode: 'rewind'; selected: number; options: RewindOption[] }

export type SelectorDecision = {
  selector: SelectorState | null
  command?: BridgeCommand
  input?: string
}

export type VisibleSelectorRow = {
  index: number
  selected: boolean
}

export type SelectorKey = {
  upArrow?: boolean
  downArrow?: boolean
  return?: boolean
  escape?: boolean
}

export function rewindOptions(messages: ChatMessage[]): RewindOption[] {
  return messages
    .filter((message): message is ChatMessage & { taskId: number } => (
      message.role === 'user' && typeof message.taskId === 'number'
    ))
    .map(message => ({ taskId: message.taskId, text: message.text }))
}

export function moveSelection(selector: SelectorState, delta: number): SelectorState {
  const size = selector.mode === 'resume' ? selector.sessions.length : selector.options.length
  if (size <= 0) return selector
  const selected = Math.max(0, Math.min(size - 1, selector.selected + delta))
  return { ...selector, selected } as SelectorState
}

export function selectorSize(selector: SelectorState): number {
  return selector.mode === 'resume' ? selector.sessions.length : selector.options.length
}

export function visibleSelectorRows(selector: SelectorState, maxRows: number): VisibleSelectorRow[] {
  const size = selectorSize(selector)
  if (size <= 0) return []
  const limit = Math.max(1, Math.min(size, Math.floor(maxRows)))
  const selected = Math.max(0, Math.min(size - 1, selector.selected))
  let start = selected - Math.floor(limit / 2)
  start = Math.max(0, Math.min(size - limit, start))
  return Array.from({ length: limit }, (_, offset) => {
    const index = start + offset
    return { index, selected: index === selected }
  })
}

export function newestResumeSessions(sessions: ResumeSession[]): ResumeSession[] {
  return [...sessions].sort((left, right) => right.mtime - left.mtime)
}

export function handleSelectorInput(selector: SelectorState, key: SelectorKey): SelectorDecision {
  if (key.escape) return { selector: null }
  if (key.upArrow) return { selector: moveSelection(selector, -1) }
  if (key.downArrow) return { selector: moveSelection(selector, 1) }
  if (!key.return) return { selector }
  if (selector.mode === 'resume') {
    const session = selector.sessions[selector.selected]
    if (!session) return { selector: null }
    return { selector: null, command: { type: 'resume_session', id: session.id } }
  }
  const option = selector.options[selector.selected]
  if (!option) return { selector: null }
  return {
    selector: null,
    command: { type: 'rewind', taskId: option.taskId },
    input: option.text,
  }
}
