import type { BridgeEvent, PermissionMode } from './protocol.js'

/**
 * 三档权限面板（read_only / ask / full_access）。
 *
 * 对齐 Codex：切到 Full Access 需二次确认（其余档直接生效）。面板状态机因此有两态：
 * - `list`：档位列表，Up/Down 选择，Enter 应用（若目标是 full_access 且当前不是，则转 confirm）
 * - `confirm`：Full Access 二次确认，Enter 确认发送，Esc 退回 list
 *
 * 纯函数放这里便于单测；App 只做 setState + 发命令的粘合。
 */

export type PermissionModeOption = {
  mode: string
  title: string
  description: string
}

export type PermissionPanelState = {
  options: PermissionModeOption[]
  selected: number
  current: string
  /** 非空表示正在对该目标档做二次确认（仅 full_access 用）。 */
  confirming: string | null
}

export const PERMISSION_MODE_META: Record<PermissionMode, { title: string; description: string }> = {
  read_only: {
    title: 'Read Only',
    description: '只读：只能看，不能改、不能跑（写/执行/有副作用工具被拒绝）',
  },
  ask: {
    title: 'Ask for approval',
    description: '要改/跑先问我：只读放行，写/执行前请求批准',
  },
  full_access: {
    title: 'Full Access',
    description: '直接干：所有工具放行（进档前二次确认）',
  },
}

function metaFor(mode: string): { title: string; description: string } {
  return (
    PERMISSION_MODE_META[mode as PermissionMode] ?? { title: mode, description: '' }
  )
}

export function panelFromPermissionStatus(
  event: Extract<BridgeEvent, { type: 'permission_status' }>,
): PermissionPanelState {
  const modes = event.modes.length > 0 ? event.modes : ['read_only', 'ask', 'full_access']
  const options = modes.map(mode => ({ mode: String(mode), ...metaFor(String(mode)) }))
  const current = String(event.mode)
  const selectedIndex = options.findIndex(option => option.mode === current)
  return {
    options,
    selected: Math.max(0, selectedIndex),
    current,
    confirming: null,
  }
}

export function movePermissionSelection(selected: number, delta: number, total: number): number {
  if (total <= 0) return 0
  return Math.max(0, Math.min(total - 1, selected + delta))
}

export function permissionPanelRows(panel: PermissionPanelState): number {
  // 标题 + 每档 2 行（标题+说明）+ 底部提示行；确认态多一行警告。
  return 1 + panel.options.length * 2 + 1 + (panel.confirming ? 1 : 0)
}

/** full_access 需要二次确认；其余档（或已在 full_access）直接生效。 */
export function requiresConfirmation(targetMode: string, currentMode: string): boolean {
  return targetMode === 'full_access' && currentMode !== 'full_access'
}

export type PermissionPanelAction =
  | { type: 'confirm'; mode: string }
  | { type: 'apply'; mode: string }
  | { type: 'noop' }

/**
 * 面板在 Enter 时的决策：
 * - 确认态：确认目标档 → apply
 * - 列表态：目标==当前 → noop（无变化，直接关）；需确认 → confirm；否则 apply
 */
export function permissionPanelOnEnter(panel: PermissionPanelState): PermissionPanelAction {
  if (panel.confirming) {
    return { type: 'apply', mode: panel.confirming }
  }
  const target = panel.options[panel.selected]
  if (!target) return { type: 'noop' }
  if (target.mode === panel.current) return { type: 'noop' }
  if (requiresConfirmation(target.mode, panel.current)) {
    return { type: 'confirm', mode: target.mode }
  }
  return { type: 'apply', mode: target.mode }
}

/** Esc：确认态退回列表；列表态关闭面板（返回 null）。 */
export function permissionPanelOnEscape(
  panel: PermissionPanelState,
): PermissionPanelState | null {
  if (panel.confirming) {
    return { ...panel, confirming: null }
  }
  return null
}

export function shouldApplyPermissionStatus(requested: boolean, panelOpen: boolean): boolean {
  return requested || panelOpen
}
