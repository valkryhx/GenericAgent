export type PasteStore = Map<number, string>

const pasteRefPattern = /\[Copied text #(\d+) \+\d+ lines\]/g

export function createPasteStore(): PasteStore {
  return new Map()
}

export function foldPastedText(text: string, store: PasteStore): string {
  const normalized = text.replace(/\r\n/g, '\n').replace(/\r/g, '\n')
  const newlineCount = (normalized.match(/\n/g) ?? []).length
  if (newlineCount === 0) return normalized
  const id = store.size + 1
  store.set(id, normalized)
  return `[Copied text #${id} +${newlineCount} lines]`
}

export function expandPastedTextRefs(text: string, store: PasteStore): string {
  return text.replace(pasteRefPattern, (match, idText) => {
    const value = store.get(Number(idText))
    return value ?? match
  })
}
