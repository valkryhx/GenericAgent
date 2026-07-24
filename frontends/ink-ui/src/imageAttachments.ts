/** Ink composer 图片附件：placeholder ↔ 本地 path（对齐 Codex LocalImage / CC pastedContents）。 */

export type ImageAttachment = {
  id: number
  path: string
  placeholder: string
  source: 'clipboard' | 'path' | 'upload' | 'cli'
}

export type ImageAttachmentStore = {
  nextId: number
  byId: Map<number, ImageAttachment>
}

export function createImageAttachmentStore(): ImageAttachmentStore {
  return { nextId: 1, byId: new Map() }
}

export function formatImagePlaceholder(id: number): string {
  return `[Image #${id}]`
}

const PLACEHOLDER_RE = /\[Image #(\d+)\]/g

export function attachImage(
  store: ImageAttachmentStore,
  path: string,
  source: ImageAttachment['source'] = 'path',
): ImageAttachment {
  const id = store.nextId++
  const item: ImageAttachment = {
    id,
    path,
    placeholder: formatImagePlaceholder(id),
    source,
  }
  store.byId.set(id, item)
  return item
}

export function pruneImageAttachments(store: ImageAttachmentStore, text: string): void {
  const referenced = new Set<number>()
  PLACEHOLDER_RE.lastIndex = 0
  let m: RegExpExecArray | null
  while ((m = PLACEHOLDER_RE.exec(text)) !== null) {
    referenced.add(Number(m[1]))
  }
  for (const id of [...store.byId.keys()]) {
    if (!referenced.has(id)) store.byId.delete(id)
  }
}

export function attachmentsForSubmit(
  store: ImageAttachmentStore,
  text: string,
): Array<{ path: string; placeholder: string; source: string }> {
  pruneImageAttachments(store, text)
  const ordered: Array<{ path: string; placeholder: string; source: string }> = []
  PLACEHOLDER_RE.lastIndex = 0
  let m: RegExpExecArray | null
  const seen = new Set<number>()
  while ((m = PLACEHOLDER_RE.exec(text)) !== null) {
    const id = Number(m[1])
    if (seen.has(id)) continue
    seen.add(id)
    const item = store.byId.get(id)
    if (item) {
      ordered.push({ path: item.path, placeholder: item.placeholder, source: item.source })
    }
  }
  return ordered
}
