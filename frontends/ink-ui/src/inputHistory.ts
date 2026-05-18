export type InputHistoryState = {
  entries: string[]
  index: number | null
  draft: string
}

export type InputHistoryResult = {
  history: InputHistoryState
  value: string
}

export function createInputHistory(): InputHistoryState {
  return { entries: [], index: null, draft: '' }
}

export function recordInput(history: InputHistoryState, value: string): InputHistoryState {
  const entry = value.trimEnd()
  if (!entry.trim()) {
    return { ...history, index: null, draft: '' }
  }
  const entries = history.entries[history.entries.length - 1] === entry
    ? history.entries
    : [...history.entries, entry]
  return { entries, index: null, draft: '' }
}

export function previousInput(history: InputHistoryState, current: string): InputHistoryResult {
  if (history.entries.length === 0) return { history, value: current }
  if (history.index === null) {
    const index = history.entries.length - 1
    return {
      history: { ...history, index, draft: current },
      value: history.entries[index] ?? current,
    }
  }
  const index = Math.max(0, history.index - 1)
  return {
    history: { ...history, index },
    value: history.entries[index] ?? current,
  }
}

export function nextInput(history: InputHistoryState, current: string): InputHistoryResult {
  if (history.index === null) return { history, value: current }
  if (history.index < history.entries.length - 1) {
    const index = history.index + 1
    return {
      history: { ...history, index },
      value: history.entries[index] ?? current,
    }
  }
  return {
    history: { ...history, index: null, draft: '' },
    value: history.draft,
  }
}
