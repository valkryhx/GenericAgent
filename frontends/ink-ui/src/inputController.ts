import type { BridgeCommand } from './protocol.js'
import { compactPasteRefs, appendFoldedText, expandPastedTextRefs, flushPendingPaste, type PasteStore } from './paste.js'
import { clampGraphemeOffset, nextGraphemeOffset, previousGraphemeOffset } from './terminalText.js'
import {
  attachImage,
  attachmentsForSubmit,
  type ImageAttachmentStore,
} from './imageAttachments.js'
import {
  asImageFilePath,
  completeAbsoluteImagePathAtCursor,
  extractImagePathCandidates,
  replaceImagePathsInText,
} from './imagePathDetect.js'
import { captureClipboardImage, captureClipboardImageAsync } from './clipboardImage.js'

export type InputKey = {
  ctrl?: boolean
  meta?: boolean
  alt?: boolean
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
  action?: { type: 'open_resume' | 'open_rewind' | 'open_mcp' | 'open_model' | 'open_permissions' | 'open_theme' | 'clear' | 'help' | 'status' }
  /** 异步贴图进行中：App 侧应吞掉快捷键并 await capture，不阻塞 stdin 循环。 */
  pendingImagePaste?: boolean
  exit?: boolean
}

function clampCursorOffset(value: string, cursorOffset: number): number {
  return clampGraphemeOffset(value, cursorOffset)
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
  if (/^\/(?:permissions|permission|perms)$/.test(trimmed)) return { action: { type: 'open_permissions' } }
  const permissionSwitch = /^\/(?:permissions|permission|perms)\s+(.+)$/.exec(trimmed)
  if (permissionSwitch) {
    const raw = permissionSwitch[1].trim().toLowerCase().replace(/[\s-]+/g, '_')
    if (raw === '?' || raw === 'help' || raw === 'status') return { command: { type: 'permission_status' } }
    const alias: Record<string, 'read_only' | 'ask' | 'full_access'> = {
      read_only: 'read_only',
      readonly: 'read_only',
      read: 'read_only',
      ask: 'ask',
      approval: 'ask',
      full_access: 'full_access',
      full: 'full_access',
      fullaccess: 'full_access',
    }
    const mode = alias[raw]
    if (mode) return { command: { type: 'set_permission_mode', mode } }
    return { command: { type: 'permission_status' } }
  }
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
  if (trimmed === '/workflows' || trimmed === '/workflow' || trimmed === '/workflow list') return { command: { type: 'workflow_list' } }
  // approve/deny were removed from the product path; swallow them so they are not sent as chat.
  if (/^\/workflow\s+(approve|deny)\b/i.test(trimmed)) return {}
  const workflowAction = /^\/workflow\s+(detail|resume|stop)\s+(\S+)(?:\s+([\s\S]*))?$/.exec(trimmed)
  if (workflowAction) {
    const action = workflowAction[1]
    const runId = workflowAction[2]
    const reason = workflowAction[3]?.trim()
    if (action === 'detail') return { command: { type: 'workflow_detail', runId } }
    if (action === 'resume') return { command: { type: 'workflow_resume', runId } }
    return { command: { type: 'workflow_stop', runId, ...(reason ? { reason } : {}) } }
  }
  // Single start path: /workflow <task> (and legacy /workflow plan <task>)
  const workflowStart = parseWorkflowStartCommand(trimmed)
  if (workflowStart) return { command: workflowStart }
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

function parseWorkflowStartCommand(trimmed: string): BridgeCommand | null {
  // Prefer bare /workflow <task>. Accept legacy "/workflow plan <task>" as the same start path.
  let rest = ''
  if (trimmed === '/workflow plan' || trimmed.startsWith('/workflow plan ')) {
    rest = trimmed.slice('/workflow plan'.length).trim()
  } else if (trimmed.startsWith('/workflow ')) {
    rest = trimmed.slice('/workflow '.length).trim()
  } else {
    return null
  }
  if (!rest) {
    return { type: 'workflow_plan', taskText: '', autoApprove: true }
  }
  // Reserved control verbs are handled above; never treat them as free-form tasks.
  const first = rest.split(/\s+/, 1)[0] || ''
  if (['list', 'detail', 'resume', 'stop', 'approve', 'deny', 'plan'].includes(first)) {
    return null
  }
  const tokens = rest.split(/\s+/).filter(Boolean)
  let timeoutSeconds: number | undefined
  const taskParts: string[] = []
  for (let index = 0; index < tokens.length; index++) {
    const token = tokens[index]
    // --manual is intentionally ignored: workflows always auto-start.
    if (token === '--manual') continue
    if (token === '--timeout') {
      const rawTimeout = tokens[index + 1]
      const parsed = rawTimeout ? Number(rawTimeout) : NaN
      if (Number.isFinite(parsed) && parsed > 0) {
        timeoutSeconds = parsed
        index += 1
        continue
      }
    }
    taskParts.push(token)
  }
  const command: BridgeCommand = { type: 'workflow_plan', taskText: taskParts.join(' '), autoApprove: true }
  if (timeoutSeconds !== undefined) command.timeoutSeconds = timeoutSeconds
  return command
}

function insertImagePlaceholdersAtCursor(
  value: string,
  cursorOffset: number,
  paths: string[],
  imageStore: ImageAttachmentStore,
  source: 'clipboard' | 'path' = 'path',
): { value: string; cursorOffset: number } {
  let nextValue = value
  let nextOffset = cursorOffset
  for (const path of paths) {
    const att = attachImage(imageStore, path, source)
    const piece = (nextOffset > 0 && !/\s$/.test(nextValue.slice(0, nextOffset)) ? ' ' : '') + att.placeholder
    nextValue = nextValue.slice(0, nextOffset) + piece + nextValue.slice(nextOffset)
    nextOffset += piece.length
  }
  return { value: nextValue, cursorOffset: nextOffset }
}

/**
 * 是否「主动贴图」快捷键。
 *
 * - Ctrl+V：Codex 绑定；但 Windows 终端常拦截，应用可能收不到
 * - Alt+V / Meta+V：Claude Code 在 Windows 的默认 imagePaste（规避系统 Ctrl+V）
 * - Ctrl+Alt+V：Codex 在 WSL/拦截场景的备用
 *
 * 解析后 rawInput 对 Ctrl+V 常为 'v'（\x16 已映射），Alt+V 为 'v' 且 meta/alt。
 */
export function isImagePasteShortcut(key: InputKey, rawInput: string): boolean {
  const seq = key.sequence ?? ''
  const isVChar =
    rawInput === 'v' ||
    rawInput === 'V' ||
    rawInput === '\x16' ||
    seq === '\x16' ||
    seq === 'v' ||
    seq === 'V'
  if (!isVChar) return false

  const alt = Boolean(key.alt || key.meta)
  const ctrl = Boolean(key.ctrl)

  // Alt+V or Meta+V (CC Windows default)
  if (alt && !ctrl) return true
  // Ctrl+V (Codex primary; may not reach app on Win)
  if (ctrl && !alt) return true
  // Ctrl+Alt+V (Codex secondary)
  if (ctrl && alt) return true
  return false
}

/**
 * 同步贴图（单测/脚本）。UI 热路径请走 handleInput 的 pendingImagePaste + applyClipboardImagePaste。
 */
export function tryPasteClipboardImage(
  value: string,
  cursorOffset: number,
  imageStore: ImageAttachmentStore,
): { value: string; cursorOffset: number; ok: boolean; error?: string } {
  const cap = captureClipboardImage()
  if (!cap.ok) {
    return { value, cursorOffset, ok: false, error: cap.error }
  }
  const inserted = insertImagePlaceholdersAtCursor(value, cursorOffset, [cap.path], imageStore, 'clipboard')
  return { ...inserted, ok: true }
}

/** 异步贴图结果应用到当前输入（捕获完成后由 App 调用）。 */
export async function applyClipboardImagePaste(
  value: string,
  cursorOffset: number,
  imageStore: ImageAttachmentStore,
): Promise<{ value: string; cursorOffset: number; ok: boolean; error?: string }> {
  const cap = await captureClipboardImageAsync()
  if (!cap.ok) {
    return { value, cursorOffset, ok: false, error: cap.error }
  }
  const inserted = insertImagePlaceholdersAtCursor(value, cursorOffset, [cap.path], imageStore, 'clipboard')
  return { ...inserted, ok: true }
}

export function handleInput(
  value: string,
  rawInput: string,
  key: InputKey,
  status: InputStatus,
  pasteStore: PasteStore,
  skillNames: ReadonlySet<string> = new Set(),
  cursorOffset?: number,
  imageStore?: ImageAttachmentStore,
): InputDecision {
  const includeCursorOffset = cursorOffset !== undefined
  const offset = clampCursorOffset(value, cursorOffset ?? value.length)
  const decision = (
    nextValue: string,
    nextCursorOffset = nextValue.length,
    extra: Omit<InputDecision, 'value' | 'cursorOffset'> = {},
  ) => makeDecision(nextValue, nextCursorOffset, includeCursorOffset, extra)

  if (key.ctrl && (rawInput === 'c' || rawInput === '')) {
    return decision(value, offset, { command: { type: 'shutdown' }, exit: true })
  }
  // 剪贴板贴图：Ctrl+V / Alt+V / Ctrl+Alt+V（见 isImagePasteShortcut）
  // UI 侧异步捕获（不 spawnSync），这里只声明 pending，避免卡死 Ink 事件循环与光标重绘。
  if (imageStore && isImagePasteShortcut(key, rawInput)) {
    return decision(value, offset, { pendingImagePaste: true })
  }
  if (key.escape) {
    return status === 'running' || status === 'stopping'
      ? decision(value, offset, { command: { type: 'stop' } })
      : decision(value, offset)
  }
  if (key.leftArrow) {
    return decision(value, previousGraphemeOffset(value, offset))
  }
  if (key.rightArrow) {
    return decision(value, nextGraphemeOffset(value, offset))
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
    const previousOffset = previousGraphemeOffset(value, offset)
    return decision(value.slice(0, previousOffset) + value.slice(offset), previousOffset)
  }
  if (key.delete && (key.sequence === '\x7f' || key.sequence === '\b')) {
    if (offset <= 0) return decision(value, offset)
    const previousOffset = previousGraphemeOffset(value, offset)
    return decision(value.slice(0, previousOffset) + value.slice(offset), previousOffset)
  }
  if (key.delete) {
    if (offset >= value.length) return decision(value, offset)
    const nextOffset = nextGraphemeOffset(value, offset)
    return decision(value.slice(0, offset) + value.slice(nextOffset), offset)
  }
  if (key.return) {
    if (value.endsWith('\\') && (!includeCursorOffset || offset === value.length)) {
      const nextValue = `${value.slice(0, -1)}\n`
      return decision(nextValue, nextValue.length)
    }
    const prepared = flushPendingPaste(compactPasteRefs(value, pasteStore), pasteStore)
    let expanded = expandPastedTextRefs(prepared, pasteStore).trimEnd()
    // 提交前：绝对图片路径 → 附件芯片（最长优先、边界安全，禁止子串误替换）
    if (imageStore && expanded) {
      const candidates = extractImagePathCandidates(expanded)
      const replacements: Array<{ path: string; placeholder: string }> = []
      for (const p of candidates) {
        if (!expanded.includes(p)) continue
        const existing = [...imageStore.byId.values()].find(a => a.path === p)
        if (existing) {
          replacements.push({ path: p, placeholder: existing.placeholder })
          continue
        }
        const att = attachImage(imageStore, p, 'path')
        replacements.push({ path: p, placeholder: att.placeholder })
      }
      if (replacements.length) {
        expanded = replaceImagePathsInText(expanded, replacements)
      }
    }
    if (!expanded) return decision(value, offset)
    const slash = parseSlashSubmit(expanded, skillNames)
    if (slash) return decision('', 0, slash)
    if (status === 'running' || status === 'stopping' || status === 'connecting') return decision(value, offset)
    const images = imageStore ? attachmentsForSubmit(imageStore, expanded) : []
    // 清空 store 中已提交的引用（placeholder 会随 value 清空）
    if (imageStore) {
      for (const id of [...imageStore.byId.keys()]) imageStore.byId.delete(id)
    }
    return decision('', 0, {
      command: images.length
        ? { type: 'submit', text: expanded, images }
        : { type: 'submit', text: expanded },
    })
  }
  if (rawInput) {
    // 粘贴：绝对图片路径 → 芯片；分片粘贴时尝试与光标前文本拼成完整绝对路径
    if (imageStore) {
      const completed = completeAbsoluteImagePathAtCursor(value, offset, rawInput)
      if (completed) {
        const before = value.slice(0, completed.start)
        const after = value.slice(offset)
        // rawInput 可能比路径尾部长（路径后还有说明文字）
        const combined = value.slice(0, offset) + rawInput
        const tail = combined.slice(completed.end)
        const att = attachImage(imageStore, completed.path, 'path')
        const spacerBefore = before.length > 0 && !/\s$/.test(before) ? ' ' : ''
        const mid = spacerBefore + att.placeholder
        const spacerAfter = tail && !/^\s/.test(tail) && !/^\s/.test(after) ? ' ' : ''
        // after 是光标后原文；tail 含本次 chunk 中路径之后的新字符
        // 光标后原文应保留，chunk 内路径后的文字用 tail
        const nextValue = before + mid + (tail ? spacerAfter + tail : '') + after
        const nextOffset = (before + mid + (tail ? spacerAfter + tail : '')).length
        return decision(nextValue, nextOffset)
      }
      const onlyPath = asImageFilePath(rawInput)
      if (onlyPath) {
        const inserted = insertImagePlaceholdersAtCursor(value, offset, [onlyPath], imageStore, 'path')
        return decision(inserted.value, inserted.cursorOffset)
      }
      const paths = extractImagePathCandidates(rawInput)
      if (paths.length > 0 && paths.join('\n').length >= rawInput.trim().length * 0.5) {
        // 整段粘贴以路径为主：先插入路径芯片，再附加剩余非路径文本
        let rest = rawInput
        const absPaths = [...paths].sort((a, b) => b.length - a.length)
        const replacements = absPaths.map(p => {
          const att = attachImage(imageStore, p, 'path')
          return { path: p, placeholder: att.placeholder }
        })
        rest = replaceImagePathsInText(rest, replacements)
        const inserted = insertLiteralTextAtCursor(value, offset, rest)
        // 若 rest 仍是纯 placeholder 串，cursor 已在末尾
        return decision(inserted.value, inserted.cursorOffset)
      }
    }
    const inserted = insertFoldedTextAtCursor(value, offset, rawInput, pasteStore)
    return decision(inserted.value, inserted.cursorOffset)
  }
  return decision(value, offset)
}
