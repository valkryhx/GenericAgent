import type { BridgeEvent } from './protocol.js'

/**
 * 主 agent ask 档：统一工具审批面板（所有工具同一套 UI）。
 *
 * 仅 accept / deny；Esc = deny。不做 session/always、不按 tool 分支组件。
 * 纯函数便于单测；App 只做 setState + 发 permission_response。
 */

export type ApprovalDecision = 'accept' | 'deny'

export type ApprovalRequest = {
  requestId: string
  toolName: string
  argsPreview: string
  reason: string
  mode: string
}

export type ApprovalPanelState = {
  current: ApprovalRequest
  /** 0 = accept, 1 = deny */
  selected: number
  queue: ApprovalRequest[]
}

export const APPROVAL_OPTIONS: { decision: ApprovalDecision; title: string }[] = [
  { decision: 'accept', title: '允许 (accept)' },
  { decision: 'deny', title: '拒绝 (deny)' },
]

export function approvalFromPermissionRequest(
  event: Extract<BridgeEvent, { type: 'permission_request' }>,
): ApprovalRequest {
  return {
    requestId: String(event.requestId || ''),
    toolName: String(event.toolName || ''),
    argsPreview: String(event.argsPreview || ''),
    reason: String(event.reason || ''),
    mode: String(event.mode || 'ask'),
  }
}

/** 新请求：无面板则创建；有面板则入队（不打断当前未决项）。 */
export function enqueueApprovalRequest(
  panel: ApprovalPanelState | null,
  request: ApprovalRequest,
): ApprovalPanelState {
  if (!panel) {
    return { current: request, selected: 0, queue: [] }
  }
  if (panel.current.requestId === request.requestId) {
    return panel
  }
  if (panel.queue.some(item => item.requestId === request.requestId)) {
    return panel
  }
  return { ...panel, queue: [...panel.queue, request] }
}

export function moveApprovalSelection(selected: number, delta: number): number {
  const total = APPROVAL_OPTIONS.length
  return Math.max(0, Math.min(total - 1, selected + delta))
}

export type ApprovalPanelAction =
  | { type: 'respond'; requestId: string; decision: ApprovalDecision; next: ApprovalPanelState | null }
  | { type: 'noop' }

export function approvalPanelOnEnter(panel: ApprovalPanelState): ApprovalPanelAction {
  const option = APPROVAL_OPTIONS[panel.selected] ?? APPROVAL_OPTIONS[1]
  return finishApproval(panel, option.decision)
}

/** Esc = deny（显式拒绝，禁止静默放行）。 */
export function approvalPanelOnEscape(panel: ApprovalPanelState): ApprovalPanelAction {
  return finishApproval(panel, 'deny')
}

function finishApproval(panel: ApprovalPanelState, decision: ApprovalDecision): ApprovalPanelAction {
  const requestId = panel.current.requestId
  const nextReq = panel.queue[0]
  const next: ApprovalPanelState | null = nextReq
    ? { current: nextReq, selected: 0, queue: panel.queue.slice(1) }
    : null
  return { type: 'respond', requestId, decision, next }
}

/** 服务端 settled/取消：若匹配当前项则弹出下一则或关闭。 */
export function approvalPanelOnSettled(
  panel: ApprovalPanelState | null,
  requestId: string,
): ApprovalPanelState | null {
  if (!panel) return null
  if (panel.current.requestId === requestId) {
    const nextReq = panel.queue[0]
    if (!nextReq) return null
    return { current: nextReq, selected: 0, queue: panel.queue.slice(1) }
  }
  return {
    ...panel,
    queue: panel.queue.filter(item => item.requestId !== requestId),
  }
}

export function approvalPanelRows(panel: ApprovalPanelState): number {
  // 标题 + 工具 + 参数 + reason? + 2 options + footer
  return 1 + 1 + 1 + (panel.current.reason ? 1 : 0) + APPROVAL_OPTIONS.length + 1
}
