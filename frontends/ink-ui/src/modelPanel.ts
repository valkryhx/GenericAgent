import type { BridgeEvent, ModelStatus } from './protocol.js'

export type ModelPanelState = {
  models: ModelStatus[]
  selected: number
  reasoning: ReasoningPanelState | null
}

export type ReasoningPanelState = {
  model: ModelStatus
  selected: number
}

export function panelFromModelStatus(event: Extract<BridgeEvent, { type: 'model_status' }>): ModelPanelState {
  const current = event.models.findIndex(model => model.current)
  return { models: event.models, selected: Math.max(0, current), reasoning: null }
}

export function moveModelSelection(selected: number, delta: number, total: number): number {
  if (total <= 0) return 0
  return ((selected + delta) % total + total) % total
}

export function modelPanelRows(panel: ModelPanelState): number {
  if (panel.reasoning) return (panel.reasoning.model.reasoningEfforts?.length ?? 0) + 2
  return panel.models.length + 2
}

export function shouldOpenReasoningPanel(model: ModelStatus): boolean {
  return model.reasoningEffortKnown === true && (model.reasoningEfforts?.length ?? 0) > 1
}

export function openReasoningPanel(panel: ModelPanelState, model: ModelStatus): ModelPanelState {
  const efforts = model.reasoningEfforts ?? []
  const current = efforts.findIndex(effort => effort === model.reasoningEffort)
  return {
    ...panel,
    reasoning: {
      model,
      selected: current >= 0 ? current : Math.max(0, efforts.indexOf(model.defaultReasoningEffort ?? '')),
    },
  }
}

export function moveReasoningSelection(selected: number, delta: number, total: number): number {
  if (total <= 0) return 0
  return Math.max(0, Math.min(total - 1, selected + delta))
}

export function shouldApplyModelStatus(requested: boolean, panelOpen: boolean): boolean {
  return requested || panelOpen
}
