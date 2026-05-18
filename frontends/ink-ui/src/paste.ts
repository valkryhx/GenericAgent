export type PasteStore = Map<number, string> & {
  pendingPaste?: string | null
}

const pasteRefPattern = /\[Copied text #(\d+) \+\d+ lines\]/g
const fullPasteRefPattern = /^\[Copied text #(\d+) \+\d+ lines\]$/
const trailingPasteRefPattern = /\[Copied text #(\d+) \+\d+ lines\]$/
const adjacentPasteRefPattern = /\[Copied text #(\d+) \+\d+ lines\]\[Copied text #(\d+) \+\d+ lines\]/
const ansiPattern = /\u001b\[[0-?]*[ -/]*[@-~]/g
const pasteStart = '\u001b[200~'
const pasteEnd = '\u001b[201~'

export function createPasteStore(): PasteStore {
  return new Map() as PasteStore
}

export function foldPastedText(text: string, store: PasteStore): string {
  if (!text) return ''
  if (store.pendingPaste != null) {
    const end = text.indexOf(pasteEnd)
    if (end === -1) {
      store.pendingPaste += text
      return ''
    }
    const pasted = store.pendingPaste + text.slice(0, end)
    store.pendingPaste = null
    return foldCompleteText(pasted, store) + foldPastedText(text.slice(end + pasteEnd.length), store)
  }

  const start = text.indexOf(pasteStart)
  if (start !== -1) {
    const before = foldCompleteText(text.slice(0, start), store)
    const rest = text.slice(start + pasteStart.length)
    const end = rest.indexOf(pasteEnd)
    if (end === -1) {
      store.pendingPaste = rest
      return before
    }
    return before + foldCompleteText(rest.slice(0, end), store) + foldPastedText(rest.slice(end + pasteEnd.length), store)
  }

  return foldCompleteText(text, store)
}

export function appendFoldedText(value: string, text: string, store: PasteStore): string {
  const merged = appendToTrailingPaste(value, text, store)
  if (merged !== null) return merged
  const folded = foldPastedText(text, store)
  if (!folded) return compactPasteRefs(value, store)
  return compactPasteRefs(value + folded, store)
}

function appendToTrailingPaste(value: string, text: string, store: PasteStore): string | null {
  if (text.length <= 1) return null
  const previous = trailingPasteRefPattern.exec(value)
  if (!previous) return null
  const id = Number(previous[1])
  const previousText = store.get(id)
  if (previousText === undefined) return null
  const combined = previousText + normalizePasteText(text)
  store.set(id, combined)
  const newlineCount = (combined.match(/\n/g) ?? []).length
  return `${value.slice(0, -previous[0].length)}[Copied text #${id} +${newlineCount} lines]`
}

export function flushPendingPaste(value: string, store: PasteStore): string {
  if (store.pendingPaste == null) return compactPasteRefs(value, store)
  const pending = store.pendingPaste
  store.pendingPaste = null
  return appendFoldedText(value, pending, store)
}

export function compactPasteRefs(value: string, store: PasteStore): string {
  let compacted = value
  let match = adjacentPasteRefPattern.exec(compacted)
  while (match) {
    const previousId = Number(match[1])
    const nextId = Number(match[2])
    const previousText = store.get(previousId)
    const nextText = store.get(nextId)
    if (previousText === undefined || nextText === undefined) break
    const merged = previousText + nextText
    store.set(previousId, merged)
    store.delete(nextId)
    const newlineCount = (merged.match(/\n/g) ?? []).length
    compacted = `${compacted.slice(0, match.index)}[Copied text #${previousId} +${newlineCount} lines]${compacted.slice(match.index + match[0].length)}`
    match = adjacentPasteRefPattern.exec(compacted)
  }
  return compacted
}

function nextPasteId(store: PasteStore): number {
  return Math.max(0, ...store.keys()) + 1
}

function foldCompleteText(text: string, store: PasteStore): string {
  if (!text) return ''
  const normalized = normalizePasteText(text)
  const newlineCount = (normalized.match(/\n/g) ?? []).length
  if (newlineCount === 0) return normalized
  const id = nextPasteId(store)
  store.set(id, normalized)
  return `[Copied text #${id} +${newlineCount} lines]`
}

function normalizePasteText(text: string): string {
  return text
    .replace(ansiPattern, '')
    .replace(/\r\n/g, '\n')
    .replace(/\r/g, '\n')
    .replace(/\t/g, '    ')
}

export function expandPastedTextRefs(text: string, store: PasteStore): string {
  return text.replace(pasteRefPattern, (match, idText) => {
    const value = store.get(Number(idText))
    return value ?? match
  })
}
