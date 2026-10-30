import type { BridgeEvent } from './protocol.js'

/**
 * 模型提议 goal 的一键审批面板（CC ProposeGoal，单条消息）。
 *
 * 仅 approve / reject；Esc = reject。与工具审批（approvalPanel）相互独立，
 * 但互斥展示（同一时刻只显示一个）。纯函数便于单测；App 只做 setState + 发
 * goal_proposal_response。
 */

export type GoalProposalDecision = 'approve' | 'reject'

export type GoalProposalRequest = {
  requestId: string
  condition: string
}

export type GoalProposalPanelState = {
  current: GoalProposalRequest
  /** 0 = approve, 1 = reject */
  selected: number
}

export const GOAL_PROPOSAL_OPTIONS: { decision: GoalProposalDecision; title: string }[] = [
  { decision: 'approve', title: '设定目标 (approve)' },
  { decision: 'reject', title: '不设定 (reject)' },
]

export function goalProposalFromEvent(
  event: Extract<BridgeEvent, { type: 'goal_proposal_request' }>,
): GoalProposalRequest {
  return {
    requestId: String(event.requestId || ''),
    condition: String(event.condition || ''),
  }
}

/** 新请求：无面板则创建；同一 requestId 幂等。 */
export function enqueueGoalProposal(
  panel: GoalProposalPanelState | null,
  request: GoalProposalRequest,
): GoalProposalPanelState {
  if (panel && panel.current.requestId === request.requestId) return panel
  return { current: request, selected: 0 }
}

export function moveGoalProposalSelection(selected: number, delta: number): number {
  const total = GOAL_PROPOSAL_OPTIONS.length
  return Math.max(0, Math.min(total - 1, selected + delta))
}

export type GoalProposalAction =
  | { type: 'respond'; requestId: string; decision: GoalProposalDecision }

export function goalProposalOnEnter(panel: GoalProposalPanelState): GoalProposalAction {
  const option = GOAL_PROPOSAL_OPTIONS[panel.selected] ?? GOAL_PROPOSAL_OPTIONS[1]
  return { type: 'respond', requestId: panel.current.requestId, decision: option.decision }
}

/** Esc = reject（显式拒绝，禁止静默设定）。 */
export function goalProposalOnEscape(panel: GoalProposalPanelState): GoalProposalAction {
  return { type: 'respond', requestId: panel.current.requestId, decision: 'reject' }
}

/** 服务端 settled：匹配当前项则关闭。 */
export function goalProposalOnSettled(
  panel: GoalProposalPanelState | null,
  requestId: string,
): GoalProposalPanelState | null {
  if (!panel) return null
  return panel.current.requestId === requestId ? null : panel
}

export function goalProposalRows(panel: GoalProposalPanelState): number {
  // 标题 + 条件(可能多行，按单行保守计) + 2 options + footer
  return 1 + 1 + GOAL_PROPOSAL_OPTIONS.length + 1
}
